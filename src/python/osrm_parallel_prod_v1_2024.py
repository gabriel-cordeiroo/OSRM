import boto3
import pandas as pd
import logging
import warnings
import asyncio
import timeit
import osrm
import os
import math
import numpy as np
import json
import requests
import subprocess
import shutil
import glob
import hashlib
from datetime import datetime, timedelta
from multiprocessing import Pool, cpu_count
from typing import List, NewType, Dict
from contextlib import contextmanager
from pytz import timezone

# --- CONFIGURAÇÃO DE LOG ---
logger = logging.getLogger()
logger.setLevel(logging.INFO)

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
    logger.addHandler(console_handler)

file_handler = logging.FileHandler('osrm_automation.log', mode='a') 
file_handler.setFormatter(log_formatter)
if not any(isinstance(h, logging.FileHandler) and h.baseFilename.endswith('osrm_automation.log') for h in logger.handlers):
    logger.addHandler(file_handler)

#############

LongLat = NewType("LongLat", List[float])
StartEndPair = NewType("StartEndPair", List[LongLat])

#############
# CONFIGURAÇÃO DE BUCKETS
#############

SOURCE_BUCKET = "50-ze-datalake-refined"
DESTINATION_BUCKET = "20-ze-datalake-landing"

SETUP = {
    "input_s3_base_prefix": 'data_mesh/vw_antifraud_fact_distances',
    "output_s3_base_prefix": 'osrm_distance/osrm_landing',
    "bookmark_s3_key": 'osrm_distance/control/bookmark_2024.json', 
    "start_coordinates": ["poc_longitude", "poc_latitude"],
    "end_coordinates": ["order_longitude", "order_latitude"],
    "metadata_columns": ["order_number"],
    "BATCH_SIZE": 1024*40,
    "NUM_PROCESSES": 15,
    "MAX_CONCURRENT": 30,
    "BLOCK_SIZE": 1_000_000,
    "skip_download": False,
}

# --- FUNÇÕES AUXILIARES ---

@contextmanager
def suppress_logging(level=logging.CRITICAL + 1):
    logger = logging.getLogger()
    prev_level = logger.level
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(prev_level)

@contextmanager
def suppress_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield

def check_disk_space():
    """Verifica espaço em disco e alerta se crítico"""
    disk = shutil.disk_usage('/')
    free_gb = disk.free / (1024**3)
    used_gb = disk.used / (1024**3)
    total_gb = disk.total / (1024**3)
    used_percent = (disk.used / disk.total) * 100
    
    logging.info(f"💾 Disco: {used_gb:.1f}/{total_gb:.1f}GB usado ({used_percent:.1f}%) | {free_gb:.1f}GB livres")
    
    try:
        result = subprocess.run(
            ['sudo', 'du', '-sm', '/var/lib/docker/containers/'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            docker_size = int(result.stdout.split()[0])
            if docker_size > 5000:
                logging.warning(f"⚠️  Logs do Docker grandes: {docker_size}MB")
            elif docker_size > 1000:
                logging.info(f"📊 Logs do Docker: {docker_size}MB")
    except Exception as e:
        logging.debug(f"Não foi possível verificar logs do Docker: {e}")
    
    if free_gb < 5:
        logging.error("❌ CRÍTICO: Menos de 5GB livres! Abortando.")
        return False
    elif free_gb < 15:
        logging.warning(f"⚠️  ATENÇÃO: Apenas {free_gb:.1f}GB livres")
    
    return True

def list_s3_objects(bucket, prefix=''):
    """Lista objetos S3 com metadados"""
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)
    
    files = []
    for page in page_iterator:
        if 'Contents' in page:
            for obj in page['Contents']:
                files.append({'Key': obj['Key'], 'LastModified': obj['LastModified'].isoformat()})
    return files

def generate_file_hash(filename: str, length: int = 8) -> str:
    """Gera hash único baseado no nome do arquivo fonte"""
    return hashlib.md5(filename.encode()).hexdigest()[:length]

def check_file_exists_s3(bucket: str, key: str) -> bool:
    """Verifica se arquivo existe no S3"""
    s3 = boto3.client('s3')
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except s3.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def upload_file_to_s3(file_path, bucket_name, s3_key):
    """Upload com verificação de existência"""
    s3 = boto3.client('s3')
    
    # Verifica se arquivo já existe
    if check_file_exists_s3(bucket_name, s3_key):
        logging.error(f"❌ CRÍTICO: Arquivo já existe em s3://{bucket_name}/{s3_key}")
        # Gera nome alternativo com timestamp
        timestamp_suffix = datetime.now().strftime('%Y%m%d%H%M%S%f')
        base_key = s3_key.rsplit('.', 1)[0]
        s3_key = f"{base_key}-{timestamp_suffix}.parquet"
        logging.warning(f"⚠️  Usando nome alternativo: {s3_key}")
    
    try:
        s3.upload_file(file_path, bucket_name, s3_key)
        logging.info(f"✅ Upload bem-sucedido: s3://{bucket_name}/{s3_key}")
        return True
    except FileNotFoundError:
        logging.error(f"❌ Arquivo não encontrado: {file_path}")
        return False
    except Exception as e:
        logging.error(f"❌ Erro no upload: {e}")
        return False

def parse_df(df):
    with suppress_warnings():
        df = df[SETUP["metadata_columns"] + SETUP["start_coordinates"] + SETUP["end_coordinates"]]
        float_columns = SETUP["start_coordinates"] + SETUP["end_coordinates"]
        for c in float_columns:
            df[c] = df[c].astype(float)
        return df

def make_list_of_coords(df):
    rows = df.to_dict(orient='records')
    return rows

# --- FUNÇÕES DE BOOKMARK E PARTIÇÕES ---

def list_s3_partitions(bucket, prefix):
    """Lista partições no formato YYYY-MM"""
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    partitions = set()
    if not prefix.endswith('/'): prefix += '/'
    logging.info(f"Listando partições em s3://{bucket}/{prefix}")
    
    # ADICIONADO: Anos a processar
    years_to_process = [2024, 2025]  # ← MUDANÇA AQUI
    
    try:
        for year in years_to_process:
            year_prefix = f"{prefix}{year}-"
            for result in paginator.paginate(Bucket=bucket, Prefix=year_prefix, Delimiter='/'):
                for common_prefix in result.get('CommonPrefixes', []):
                    partition_name = common_prefix.get('Prefix').split('/')[-2]
                    if len(partition_name) == 7 and partition_name[4] == '-':
                        partitions.add(partition_name)
        return sorted(list(partitions))
    except Exception as e:
        logging.error(f"Erro ao listar partições S3: {e}")
        return []

def get_processed_bookmark(bucket, key) -> Dict:
    """Lê bookmark com histórico e deltas"""
    s3 = boto3.client('s3')
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        bookmark = json.loads(obj['Body'].read().decode('utf-8'))
        return bookmark
    except s3.exceptions.NoSuchKey:
        logging.warning(f"Arquivo de bookmark não encontrado. Criando novo.")
        return {"completed_partitions": [], "delta_timestamps": {}}
    except Exception as e:
        logging.error(f"Erro ao ler bookmark: {e}")
        return {"completed_partitions": [], "delta_timestamps": {}}

def update_processed_bookmark(bucket, key, completed_partition: str = None, delta_timestamp: str = None, partition_name: str = None):
    """Atualiza bookmark com validação"""
    s3 = boto3.client('s3')
    bookmark = get_processed_bookmark(bucket, key)
    
    if completed_partition:
        if completed_partition not in bookmark.get("completed_partitions", []):
             bookmark["completed_partitions"].append(completed_partition)
             bookmark["completed_partitions"].sort()
        bookmark["delta_timestamps"].pop(completed_partition, None)

    if delta_timestamp and partition_name:
        current_ts = bookmark["delta_timestamps"].get(partition_name)
        if not current_ts or delta_timestamp > current_ts:
            bookmark["delta_timestamps"][partition_name] = delta_timestamp
    
    # Adiciona timestamp da última atualização
    bookmark["last_updated"] = datetime.now(timezone('UTC')).isoformat()
    
    s3.put_object(
        Bucket=bucket, Key=key, Body=json.dumps(bookmark, indent=2)
    )
    logging.info(f"✅ Bookmark atualizado em s3://{bucket}/{key}")
    
    # VERIFICAÇÃO: Confirma que foi salvo corretamente
    verified = get_processed_bookmark(bucket, key)
    if delta_timestamp and partition_name:
        saved_ts = verified.get("delta_timestamps", {}).get(partition_name)
        if saved_ts != delta_timestamp:
            logging.error(f"❌ CRÍTICO: Bookmark não foi salvo corretamente!")
            logging.error(f"   Esperado: {delta_timestamp}")
            logging.error(f"   Salvo: {saved_ts}")
            raise Exception("Falha na verificação do bookmark")
    
    logging.info(f"✅ Bookmark verificado e confirmado")

def delete_s3_prefix(bucket, prefix):
    """Deleta objetos com prefixo específico"""
    s3 = boto3.client('s3')
    if not prefix.endswith('/'): prefix += '/'
    logging.warning(f"🗑️  INICIANDO EXCLUSÃO de s3://{bucket}/{prefix}")
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    objects_to_delete = []
    try:
        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    objects_to_delete.append({'Key': obj['Key']})
                    if len(objects_to_delete) == 1000:
                        s3.delete_objects(Bucket=bucket, Delete={'Objects': objects_to_delete})
                        logging.info("Excluído lote de 1000 objetos.")
                        objects_to_delete = []
        if objects_to_delete:
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objects_to_delete})
            logging.info(f"Excluído lote final de {len(objects_to_delete)} objetos.")
        logging.info(f"✅ EXCLUSÃO CONCLUÍDA de s3://{bucket}/{prefix}")
    except Exception as e:
        logging.error(f"Erro ao excluir objetos do S3: {e}")
        raise

# --- OSRM ---

async def get_client() -> osrm.AioHTTPClient:
    return osrm.AioHTTPClient(host='http://localhost:5000', max_retries=10, timeout=10)

async def async_request(point, client: osrm.AioHTTPClient = None, max_retries: int = 5):
    start_coords = [point[c] for c in SETUP["start_coordinates"]]
    end_coords = [point[c] for c in SETUP["end_coordinates"]]
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.route(coordinates=[start_coords, end_coords], overview=osrm.overview.false)
            return {
                **{k: point[k] for k in SETUP["metadata_columns"]},
                "distance": float(response['routes'][0]['distance']),
                "duration": float(response['routes'][0]['duration'])
            }
        except Exception as e:
            logging.error(f"Error OSRM. {e}. Coords: {start_coords} -> {end_coords}")
            start_coords = [np.round(x, 4) for x in start_coords]
            end_coords = [np.round(x, 4) for x in end_coords]
            if attempt < max_retries:
                await asyncio.sleep(0.1 * (2 ** attempt))
                if "disconnected" in str(e).lower(): client = await get_client()
                if "no route" in str(e).lower(): return None
            else:
                logging.error(f"Falha permanente após {max_retries} tentativas.")
                return None

async def batch_request(points: List[StartEndPair], max_concurrent = 100):
    client = await get_client()
    semaphore = asyncio.Semaphore(max_concurrent)
    async def limited_request(point):
        async with semaphore: return await async_request(point, client)
    tasks = [limited_request(point) for point in points]
    output = await asyncio.gather(*tasks)
    await client.close()
    output = [x for x in output if x is not None]
    return output

def process_chunk(chunk: List[StartEndPair], max_concurrent = 100):
    return asyncio.run(batch_request(chunk, max_concurrent=max_concurrent))

def chunk_list(lst, n):
    k, m = divmod(len(lst), n)
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]

def parallel_osrm_requests(points: List[StartEndPair], num_processes=None, max_concurrent=100):
    if num_processes is None: num_processes = cpu_count()
    chunks = chunk_list(points, num_processes)
    with Pool(processes=num_processes) as pool:
        results_nested = pool.starmap(process_chunk, [(chunk, max_concurrent) for chunk in chunks])
    return [item for sublist in results_nested for item in sublist]

def shutdown_instance():
    """Auto-desliga a instância com tratamento robusto de erros"""
    logging.info("Nenhum trabalho restante. Iniciando auto-desligamento da instância...")
    try:
        r = requests.get('http://169.254.169.254/latest/meta-data/instance-id', timeout=10)
        if r.status_code != 200 or not r.text:
            logging.error(f"❌ Falha ao obter instance-id. Status: {r.status_code}")
            return
        
        instance_id = r.text.strip()
        
        r = requests.get('http://169.254.169.254/latest/dynamic/instance-identity/document', timeout=10)
        if r.status_code != 200:
            logging.error(f"❌ Falha ao obter documento de identidade. Status: {r.status_code}")
            return
        
        try:
            identity = r.json()
            region = identity.get('region')
            if not region:
                logging.error("❌ Região não encontrada no documento de identidade.")
                return
        except json.JSONDecodeError:
            logging.error(f"❌ Resposta inválida da API de metadados: {r.text[:100]}")
            return
        
        logging.info(f"Desligando a instância {instance_id} na região {region}...")
        result = subprocess.run(
            ["aws", "ec2", "stop-instances", "--instance-ids", instance_id, "--region", region], 
            check=True,
            capture_output=True,
            text=True
        )
        logging.info(f"✅ Comando de desligamento executado: {result.stdout}")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Erro de rede ao acessar metadados: {e}")
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Erro ao executar comando AWS CLI: {e.stderr}")
    except Exception as e:
        logging.error(f"❌ Erro inesperado no desligamento: {e}")

# --- MAIN ---

if __name__ == "__main__":
    
    total_samples_processed = 0
    
    logging.info("="*60)
    logging.info("🚀 Iniciando pipeline de processamento OSRM")
    logging.info("="*60)
    
    # 1. VERIFICAÇÃO INICIAL
    if not check_disk_space():
        logging.error("❌ Espaço insuficiente no disco. Abortando.")
        exit(1) 
    
    # 2. LIMPEZA DE TEMPORÁRIOS
    logging.info("🧹 Verificando arquivos temporários pendentes...")
    temp_files_removed = 0
    temp_size_freed = 0
    
    for pattern in ['*.csv', '*temp_block_*.parquet']:
        for f in glob.glob(pattern):
            try:
                size = os.path.getsize(f) / (1024**2)
                os.remove(f)
                temp_files_removed += 1
                temp_size_freed += size
            except Exception as e:
                logging.warning(f"⚠️  Erro ao remover {f}: {e}")
    
    if temp_files_removed > 0:
        logging.info(f"✅ {temp_files_removed} arquivo(s) removido(s), {temp_size_freed:.1f}MB liberados")
        check_disk_space()
    else:
        logging.info("✅ Nenhum arquivo temporário encontrado")
    
    # 3. IDENTIFICAR FILA DE TRABALHO
    current_month_partition = datetime.now().strftime('%Y-%m')
    previous_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    processing_date = datetime.now().strftime('%Y-%m-%d')  # Para ingestion_date
    
    available_partitions = list_s3_partitions(SOURCE_BUCKET, SETUP["input_s3_base_prefix"])
    
    # Filtra apenas partições de 2025
    available_partitions = [p for p in available_partitions if p.startswith('2024-')]
    
    full_bookmark = get_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"])
    processed_partitions_history = set(full_bookmark.get("completed_partitions", []))
    delta_timestamps = full_bookmark.get("delta_timestamps", {})

    # LÓGICA CORRIGIDA:
    # 1. Históricos = partições não concluídas (EXCETO mês corrente)
    historical_work = set(available_partitions) - processed_partitions_history - {current_month_partition}
    
    # 2. Se mês anterior tem checkpoint ativo, NÃO é histórico
    if previous_month in delta_timestamps:
        historical_work.discard(previous_month)
    
    # 3. Ordena trabalho histórico
    partitions_to_process = sorted(list(historical_work))
    
    # 4. Adiciona mês anterior se tiver checkpoint (INCREMENTAL)
    if previous_month in available_partitions and previous_month in delta_timestamps:
        partitions_to_process.append(previous_month)
    
    # 5. Adiciona mês corrente (INCREMENTAL)
    if current_month_partition in available_partitions:
        partitions_to_process.append(current_month_partition)
    
    if not partitions_to_process:
        logging.info("✅ Nenhuma partição nova para processar. Encerrando.")
        shutdown_instance()
        exit(0)

    logging.info(f"📋 Fila de trabalho: {partitions_to_process}")
    logging.info(f"📅 Ingestion date para novos dados: {processing_date}")

    # 4. LOOP DE PROCESSAMENTO
    
    for partition_idx, partition_to_run in enumerate(partitions_to_process):
        
        # Detecta tipo de job com lógica aprimorada
        is_current_month = (partition_to_run == current_month_partition)
        is_previous_month = (partition_to_run == previous_month)
        has_checkpoint = (partition_to_run in delta_timestamps)
        
        if is_current_month:
            job_type = "INCREMENTAL (mês corrente)"
        elif is_previous_month and has_checkpoint:
            job_type = "INCREMENTAL (mês anterior com checkpoint)"
        else:
            job_type = "HISTÓRICO"
        
        logging.info("="*60)
        logging.info(f"📅 Partição {partition_idx + 1}/{len(partitions_to_process)}: {partition_to_run} ({job_type})")
        logging.info("="*60)

        input_key = os.path.join(SETUP["input_s3_base_prefix"], partition_to_run)
        
        # ESTRUTURA SIMPLES: year/month (sem processing_date)
        output_partition_path = f"year={partition_to_run[:4]}/month={partition_to_run[5:]}"
        output_s3_prefix = os.path.join(SETUP["output_s3_base_prefix"], output_partition_path)
        
        logging.info(f"📂 Path de saída: s3://{DESTINATION_BUCKET}/{output_s3_prefix}/")
        
        last_processed_ts = None
        if partition_to_run in delta_timestamps:
            last_processed_ts = datetime.fromisoformat(delta_timestamps[partition_to_run]) 
            logging.info(f"🔍 Checkpoint: Último arquivo processado em {last_processed_ts}")
        
        try:
            
            # 5. LISTAR E FILTRAR ARQUIVOS
            all_s3_files = list_s3_objects(SOURCE_BUCKET, input_key)
            
            logging.info(f"📋 Total de arquivos encontrados na source: {len(all_s3_files)}")

            files_to_download_filtered = []
            max_ts_current_run = None
            
            for file_data in all_s3_files:
                file_key = file_data['Key']
                last_mod_dt = datetime.fromisoformat(file_data['LastModified'])

                if not file_key.endswith(".parquet"):
                    continue
                
                # CORRIGIDO: Filtro para modo incremental (mês corrente OU mês anterior com checkpoint)
                is_incremental_mode = (is_current_month or (is_previous_month and has_checkpoint))
                
                if is_incremental_mode and last_processed_ts and last_mod_dt <= last_processed_ts:
                    logging.warning(f"Delta: Ignorando arquivo já processado: {os.path.basename(file_key)} ({last_mod_dt})")
                    continue
                
                files_to_download_filtered.append(file_data)
            
            # Ordenação cronológica
            files_to_download_filtered.sort(key=lambda x: datetime.fromisoformat(x['LastModified']))
            
            if files_to_download_filtered:
                max_ts_current_run = datetime.fromisoformat(files_to_download_filtered[-1]['LastModified'])
                logging.info(f"📋 Arquivos após filtro: {len(files_to_download_filtered)}")
                logging.info(f"📋 Primeiro: {os.path.basename(files_to_download_filtered[0]['Key'])} ({files_to_download_filtered[0]['LastModified']})")
                logging.info(f"📋 Último: {os.path.basename(files_to_download_filtered[-1]['Key'])} ({files_to_download_filtered[-1]['LastModified']})")

            if not files_to_download_filtered:
                logging.info(f"✅ Nenhuma atualização na partição {partition_to_run}.")
                if not is_current_month and not (is_previous_month and has_checkpoint):
                    update_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"], completed_partition=partition_to_run)
                continue

            # 6. LOOP DE PROCESSAMENTO
            
            for k_file, file_data in enumerate(files_to_download_filtered):
                
                source_filename = os.path.basename(file_data['Key'])
                local_file_path = source_filename
                
                # GERA HASH ÚNICO baseado no nome do arquivo fonte
                file_hash = generate_file_hash(source_filename.replace('.parquet', ''))
                
                logging.info(f"📂 [{k_file + 1}/{len(files_to_download_filtered)}] Processando: {source_filename}")
                logging.info(f"🔑 Hash do arquivo: {file_hash}")
                
                # Download
                s3 = boto3.client('s3')
                s3.download_file(SOURCE_BUCKET, file_data['Key'], local_file_path)

                try:
                    df_full = pd.read_parquet(local_file_path)
                    
                    # DEDUPE: Remove duplicatas DENTRO do arquivo fonte
                    total_antes_dedupe = len(df_full)
                    df_full = df_full.drop_duplicates(subset=['order_number'], keep='first')
                    total_depois_dedupe = len(df_full)
                    
                    duplicatas_removidas = total_antes_dedupe - total_depois_dedupe
                    if duplicatas_removidas > 0:
                        logging.warning(f"⚠️  Removidas {duplicatas_removidas:,} duplicatas do arquivo fonte!")
                    
                except Exception as e:
                    logging.error(f"❌ Erro ao ler Parquet {local_file_path}: {e}")
                    if os.path.exists(local_file_path): os.remove(local_file_path)
                    continue
                
                num_records = len(df_full)
                logging.info(f"📊 Total de registros no arquivo (após dedupe): {num_records:,}")
                
                for k_chunk, i in enumerate(range(0, num_records, SETUP["BLOCK_SIZE"])):
                    
                    chunk = df_full[i:i + SETUP["BLOCK_SIZE"]]
                    
                    logging.info(f"⚙️  Processando bloco {k_chunk + 1} (Tamanho: {len(chunk):,})...")

                    chunk = parse_df(chunk)
                    chunk = chunk.dropna(subset=SETUP["start_coordinates"]+SETUP["end_coordinates"])
                    coords_list = make_list_of_coords(chunk)

                    if not coords_list:
                        logging.warning(f"⚠️  Bloco {k_chunk + 1} vazio após limpeza. Pulando.")
                        continue

                    start_time = timeit.default_timer()
                    _output = parallel_osrm_requests(coords_list, num_processes=SETUP['NUM_PROCESSES'], max_concurrent=SETUP['MAX_CONCURRENT'])
                    end_time = timeit.default_timer()
                    logging.info(f"✅ Bloco {k_chunk + 1} processado em {(end_time - start_time):.2f} seg.")
                    
                    if not _output:
                        logging.warning(f"⚠️  Bloco {k_chunk + 1} não produziu resultados. Pulando.")
                        continue
                    
                    output_df = pd.DataFrame(_output)
                    
                    # Adiciona metadados
                    output_df['ingestion_date'] = processing_date
                    output_df['processing_timestamp'] = datetime.now().isoformat()
                    output_df['source_file'] = source_filename
                    
                    stats = {
                        "nan": len(output_df[output_df["distance"].isna()]), 
                        "zero": len(output_df[output_df["distance"] == 0])
                    }
                    if stats["nan"] > 0: 
                        logging.error(f"⚠️  Atenção: {stats['nan']} valores nulos no bloco {k_chunk + 1}.")
                    logging.info(f"📊 Bloco {k_chunk + 1}: {stats['zero']} rotas com distância zero.")
                    
                    # 7. UPLOAD COM NOME ÚNICO
                    local_temp_parquet = f"temp_block_{file_hash}_{k_chunk}.parquet"
                    output_df.to_parquet(local_temp_parquet, index=False, engine='pyarrow')
                    
                    # NOME ÚNICO: part-{hash}-{file_idx}-{chunk}.parquet
                    output_s3_key = f"{output_s3_prefix}/part-{file_hash}-{k_file:03d}-{k_chunk:05d}.parquet"

                    if not upload_file_to_s3(local_temp_parquet, DESTINATION_BUCKET, output_s3_key):
                        raise Exception(f"Falha ao fazer upload para {output_s3_key}")

                    os.remove(local_temp_parquet)
                    total_samples_processed += len(output_df)
                    logging.info(f"💾 [{total_samples_processed:,} processados] Salvo: {os.path.basename(output_s3_key)}")
                
                # Limpeza do arquivo fonte
                if os.path.exists(local_file_path): 
                    os.remove(local_file_path)
                    logging.info(f"🗑️  Arquivo de entrada deletado: {local_file_path}")

            logging.info(f"✅ Partição {partition_to_run} concluída. Total: {total_samples_processed:,} registros.")

            # 8. ATUALIZAR BOOKMARK
            if is_current_month or (is_previous_month and has_checkpoint):
                # Modo incremental: atualiza delta_timestamp
                update_processed_bookmark(
                    DESTINATION_BUCKET, 
                    SETUP["bookmark_s3_key"], 
                    delta_timestamp=max_ts_current_run.isoformat(), 
                    partition_name=partition_to_run
                )
            else:
                # Modo histórico: marca como completo
                update_processed_bookmark(
                    DESTINATION_BUCKET, 
                    SETUP["bookmark_s3_key"], 
                    completed_partition=partition_to_run
                )

        except Exception as e:
            logging.error(f"❌ FATAL: Falha ao processar {partition_to_run}: {e}")
            
            logging.warning("🚨 Executando limpeza de emergência...")
            for f in os.listdir('.'):
                 if f.endswith(".parquet") or f.endswith(".csv"):
                     try:
                         os.remove(f)
                         logging.info(f"🗑️  Removido: {f}")
                     except:
                         pass
            
            exit(1)

    logging.info("="*60)
    logging.info("🎉 Pipeline OSRM concluído com sucesso!")
    logging.info(f"📊 Total processado: {total_samples_processed:,} registros")
    logging.info("="*60)
    check_disk_space()