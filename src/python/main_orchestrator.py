import os
import glob
import logging
import timeit
import hashlib
import pandas as pd
import boto3  # ← ADICIONADO
import shutil  # ← ADICIONADO
from datetime import datetime, timedelta

# --- Importações dos Módulos ---
from config import SOURCE_BUCKET, DESTINATION_BUCKET, SETUP, processing_date
from s3_io import (
    get_processed_bookmark, update_processed_bookmark, list_s3_partitions,
    list_s3_objects, upload_file_to_s3, load_existing_order_numbers  # ← ADICIONADO
)
from processing import (
    parallel_osrm_requests, parse_df, make_list_of_coords, 
    check_disk_space, shutdown_instance
)
# --------------------------------

# --- CONFIGURAÇÃO DE LOG ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)
if not logger.handlers:
    log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    logger.addHandler(console_handler)
    
    file_handler = logging.FileHandler('osrm_automation.log', mode='a') 
    file_handler.setFormatter(log_formatter)
    logger.addHandler(file_handler)
# ----------------------------


def generate_file_hash(filename: str, length: int = 8) -> str:
    """Gera hash único baseado no nome do arquivo fonte + timestamp."""
    unique_string = f"{filename}_{datetime.now().isoformat()}"
    return hashlib.md5(unique_string.encode()).hexdigest()[:length]

def download_partition_file(bucket_name, file_key, local_path):
    """Baixa um único arquivo."""
    s3 = boto3.client('s3')
    try:
        s3.download_file(bucket_name, file_key, local_path)
        logging.info(f"Downloaded: s3://{bucket_name}/{file_key} -> {local_path}")
        return True
    except Exception as e:
        logging.error(f"❌ Erro no download de {file_key}: {e}")
        return False

def cleanup_temp_files(local_dir):
    """Limpa o diretório temporário."""
    logging.info(f"🗑️  Limpando diretório temporário: {local_dir}")
    removed = 0
    for f in glob.glob(os.path.join(local_dir, "part-*.parquet")):
        try:
            os.remove(f)
            removed += 1
        except Exception:
            pass
    logging.info(f"✅ {removed} arquivo(s) temporário(s) removidos.")


def run_pipeline():
    
    total_samples_processed = 0
    total_duplicates_removed = 0
    
    logging.info("="*60)
    logging.info("🚀 Iniciando pipeline de processamento OSRM")
    logging.info("="*60)
    
    # 1. VERIFICAÇÃO INICIAL
    if not check_disk_space():
        logging.error("❌ Espaço insuficiente no disco. Abortando.")
        exit(1)
        
    # 2. LIMPEZA DE TEMPORÁRIOS GLOBAIS
    for pattern in ['*.csv', '*temp_block_*.parquet']:
        for f in glob.glob(pattern):
            try:
                os.remove(f)
            except Exception:
                pass
    
    # 2b. Setup e limpeza do diretório de parts
    LOCAL_TEMP_DIR = SETUP.get("LOCAL_TEMP_DIR")
    os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
    cleanup_temp_files(LOCAL_TEMP_DIR)
    
    # 3. IDENTIFICAR FILA DE TRABALHO
    current_month_partition = datetime.now().strftime('%Y-%m')
    
    available_partitions = list_s3_partitions(SOURCE_BUCKET, SETUP["input_s3_base_prefix"])
    
    # FILTRO: Processar apenas 2025+
    available_partitions = [p for p in available_partitions if p.startswith('2025-')]
    logging.info(f"Filtro aplicado: Processando {len(available_partitions)} partições (2025+).")
    
    full_bookmark = get_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"])
    processed_partitions_history = set(full_bookmark.get("completed_partitions", []))
    delta_timestamps = full_bookmark.get("delta_timestamps", {})

    # Lógica para definir a ordem de processamento
    historical_work = set(available_partitions) - processed_partitions_history - {current_month_partition}
    partitions_to_process = sorted(list(historical_work))
    
    if current_month_partition in available_partitions:
        partitions_to_process.append(current_month_partition)
    
    if not partitions_to_process:
        logging.info("✅ Nenhuma partição nova para processar. Encerrando.")
        shutdown_instance()
        exit(0)

    logging.info(f"📋 Fila de trabalho: {partitions_to_process}")

    # 4. LOOP DE PROCESSAMENTO
    
    for partition_to_run in partitions_to_process:
        
        is_current_month = (partition_to_run == current_month_partition)
        job_type = "INCREMENTAL (mês corrente)" if is_current_month else "HISTÓRICO"
        
        logging.info("="*60)
        logging.info(f"📅 Partição: {partition_to_run} ({job_type})")
        logging.info("="*60)

        input_key = os.path.join(SETUP["input_s3_base_prefix"], partition_to_run)
        output_partition_path = f"year={partition_to_run[:4]}/month={partition_to_run[5:]}"
        output_s3_prefix = os.path.join(SETUP["output_s3_base_prefix"], output_partition_path)
        
        last_processed_ts = None
        if partition_to_run in delta_timestamps:
            last_processed_ts = datetime.fromisoformat(delta_timestamps[partition_to_run]) 
        
        try:
            # 5. LISTAR E FILTRAR ARQUIVOS
            all_s3_files = list_s3_objects(SOURCE_BUCKET, input_key)
            files_to_download_filtered = []
            max_ts_current_run = None
            
            for file_data in all_s3_files:
                file_key = file_data['Key']
                last_mod_dt = datetime.fromisoformat(file_data['LastModified'])
                
                if not file_key.endswith(".parquet"): continue
                
                if is_current_month and last_processed_ts and last_mod_dt <= last_processed_ts:
                    logging.warning(f"Delta: Ignorando arquivo já processado: {os.path.basename(file_key)}")
                    continue
                
                files_to_download_filtered.append(file_data)
            
            files_to_download_filtered.sort(key=lambda x: datetime.fromisoformat(x['LastModified']))
            
            if files_to_download_filtered:
                max_ts_current_run = datetime.fromisoformat(files_to_download_filtered[-1]['LastModified'])
            else:
                logging.info(f"✅ Nenhuma atualização na partição {partition_to_run}.")
                if not is_current_month: 
                    update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], 
                                            completed_partition=partition_to_run)
                continue

            # 6. LOOP DE PROCESSAMENTO DE ARQUIVOS
            for k_file, file_data in enumerate(files_to_download_filtered):
                
                source_filename = os.path.basename(file_data['Key'])
                local_file_path = source_filename
                file_hash = generate_file_hash(source_filename.replace('.parquet', ''))
                
                if not download_partition_file(SOURCE_BUCKET, file_data['Key'], local_file_path): 
                    continue
                
                try:
                    df_full = pd.read_parquet(local_file_path)
                    df_full = df_full.drop_duplicates(subset=['order_number'], keep='first')
                except Exception as e:
                    logging.error(f"❌ Erro ao ler Parquet {local_file_path}: {e}")
                    os.remove(local_file_path)
                    continue
                
                num_records = len(df_full)
                
                for k_chunk, i in enumerate(range(0, num_records, SETUP["BLOCK_SIZE"])):
                    
                    chunk = df_full[i:i + SETUP["BLOCK_SIZE"]]
                    chunk = parse_df(chunk)
                    chunk = chunk.dropna(subset=SETUP["start_coordinates"]+SETUP["end_coordinates"])
                    coords_list = make_list_of_coords(chunk)

                    if not coords_list: continue

                    _output = parallel_osrm_requests(coords_list, 
                                                    num_processes=SETUP['NUM_PROCESSES'], 
                                                    max_concurrent=SETUP['MAX_CONCURRENT'])
                    
                    if not _output: continue
                    
                    output_df = pd.DataFrame(_output)
                    
                    # Deduplicação Garantida
                    output_df = output_df.drop_duplicates(subset=['order_number'], keep='first')
                    
                    # Adiciona metadados
                    output_df['ingestion_date'] = processing_date
                    output_df['processing_timestamp'] = datetime.now().isoformat()
                    
                    # Salvar Localmente
                    part_filename = f"part-{file_hash}-{k_file:03d}-{k_chunk:05d}.parquet"
                    local_part_path = os.path.join(LOCAL_TEMP_DIR, part_filename)
                    output_df.to_parquet(local_part_path, index=False, engine='pyarrow')
                    
                    total_samples_processed += len(output_df)
                    
                os.remove(local_file_path)
            
            # ===== 7. CONSOLIDAR E FAZER UPLOAD COM DEDUPE CROSS-FILE =====
            logging.info("="*60)
            logging.info("📦 Consolidando arquivos part-* locais...")
            logging.info("="*60)
            
            local_parts = glob.glob(os.path.join(LOCAL_TEMP_DIR, "part-*.parquet"))
            
            if local_parts:
                logging.info(f"📋 Encontrados {len(local_parts)} arquivos part-* para consolidar")
                
                # Ler todos os parts
                dfs = [pd.read_parquet(p) for p in local_parts]
                df_consolidated = pd.concat(dfs, ignore_index=True)
                
                logging.info(f"📊 Total ANTES dedupe interno: {len(df_consolidated):,} registros")
                
                # Dedupe interno
                df_consolidated = df_consolidated.drop_duplicates(subset=['order_number'], keep='first')
                
                logging.info(f"📊 Total APÓS dedupe interno: {len(df_consolidated):,} registros")
                
                # ===== DEDUPE CROSS-FILE =====
                logging.info("="*60)
                logging.info("🔍 INICIANDO DEDUPE CROSS-FILE")
                logging.info("="*60)
                
                # Carregar order_numbers já existentes
                existing_orders = load_existing_order_numbers(DESTINATION_BUCKET, output_s3_prefix)
                
                if existing_orders:
                    total_antes_cross = len(df_consolidated)
                    
                    # Filtrar: manter apenas orders que NÃO existem
                    df_consolidated = df_consolidated[~df_consolidated['order_number'].isin(existing_orders)]
                    
                    total_depois_cross = len(df_consolidated)
                    cross_duplicatas = total_antes_cross - total_depois_cross
                    
                    if cross_duplicatas > 0:
                        logging.warning(f"🗑️  Removidas {cross_duplicatas:,} duplicatas cross-file!")
                        total_duplicates_removed += cross_duplicatas
                    else:
                        logging.info(f"✅ Nenhuma duplicata cross-file encontrada")
                    
                    logging.info(f"📊 Total APÓS dedupe cross-file: {len(df_consolidated):,} registros")
                else:
                    logging.info("✅ Nenhum arquivo existente - primeira execução")
                
                # Verificar se sobrou algo
                if len(df_consolidated) == 0:
                    logging.warning("⚠️  ATENÇÃO: Todos os registros eram duplicados!")
                    logging.warning("⚠️  Nenhum dado novo para salvar. Pulando upload.")
                    cleanup_temp_files(LOCAL_TEMP_DIR)
                    
                    # Atualizar bookmark
                    if is_current_month:
                        update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], 
                                                delta_timestamp=max_ts_current_run.isoformat(), 
                                                partition_name=partition_to_run)
                    else:
                        update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], 
                                                completed_partition=partition_to_run)
                    continue
                
                logging.info("="*60)
                logging.info(f"📦 Gerando arquivo consolidado final...")
                logging.info("="*60)
                
                # Nome do arquivo
                consolidated_hash = generate_file_hash(f"final_{partition_to_run}")
                consolidated_filename = f"dedupe-{consolidated_hash}.parquet"
                local_consolidated_path = os.path.join(LOCAL_TEMP_DIR, consolidated_filename)
                
                df_consolidated.to_parquet(local_consolidated_path, index=False, engine='pyarrow')
                
                s3_consolidated_key = f"{output_s3_prefix}/{consolidated_filename}"
                
                if upload_file_to_s3(local_consolidated_path, DESTINATION_BUCKET, s3_consolidated_key):
                    logging.info(f"✅ Upload consolidado bem-sucedido!")
                    cleanup_temp_files(LOCAL_TEMP_DIR)
                    
                # 8. ATUALIZAR BOOKMARK
                if is_current_month:
                    update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], 
                                            delta_timestamp=max_ts_current_run.isoformat(), 
                                            partition_name=partition_to_run)
                else:
                    update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], 
                                            completed_partition=partition_to_run)

        except Exception as e:
            logging.error(f"❌ FATAL: Falha ao processar {partition_to_run}: {e}")
            cleanup_temp_files(LOCAL_TEMP_DIR)
            exit(1)

    logging.info("="*60)
    logging.info("🎉 Pipeline OSRM concluído com sucesso!")
    logging.info(f"🗑️  Total de duplicatas removidas: {total_duplicates_removed:,}")
    logging.info("="*60)
    shutdown_instance()

if __name__ == "__main__":
    run_pipeline()