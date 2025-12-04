# Arquivo: maintenance_ingestion_date.py
# Propósito: Adicionar coluna 'ingestion_date' retroativamente a todos os arquivos existentes

import boto3
import pandas as pd
import os
import logging
from datetime import datetime
from typing import List, Dict

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURAÇÕES ---
TARGET_BUCKET = "20-ze-datalake-landing"
TARGET_PREFIX = "osrm_distance/osrm_landing/"
INGESTION_DATE = datetime.now().strftime('%Y-%m-%d')  # Data atual: 2025-11-27

# --- FUNÇÕES AUXILIARES DE LIMPEZA ---

def cleanup_all_temp_files():
    """Remove TODOS os arquivos temporários do diretório atual (segurança)."""
    logging.warning("🚨 Executando limpeza de emergência de arquivos temporários...")
    removed = 0
    for f in os.listdir('.'):
        if f.startswith('temp_maintenance_') and f.endswith('.parquet'):
            try:
                os.remove(f)
                removed += 1
                logging.info(f"🗑️  Removido: {f}")
            except Exception as e:
                logging.error(f"Erro ao remover {f}: {e}")
    logging.info(f"✅ {removed} arquivo(s) temporário(s) removidos.")

# --- FUNÇÕES ---

def list_all_parquet_files(bucket, prefix) -> List[Dict]:
    """Lista todos os arquivos .parquet recursivamente no prefixo alvo."""
    s3 = boto3.client('s3')
    files = []
    paginator = s3.get_paginator('list_objects_v2')
    
    logging.info(f"🔍 Buscando arquivos em s3://{bucket}/{prefix}...")
    pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    
    for page in pages:
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                if key.endswith('.parquet'):
                    files.append({'Key': key, 'Size': obj['Size']})
    
    logging.info(f"✅ {len(files)} arquivos .parquet encontrados para processamento.")
    return files

def process_and_overwrite(file_list: List[Dict], bucket: str, ingestion_date: str):
    """Lê, adiciona a coluna de data e reescreve o arquivo no S3."""
    s3 = boto3.client('s3')
    
    success_count = 0
    error_count = 0
    skipped_count = 0
    
    for idx, file_data in enumerate(file_list):
        s3_key = file_data['Key']
        
        # Nome local único para evitar conflitos
        local_path = f"temp_maintenance_{idx}.parquet"
        
        logging.info(f"[{idx+1}/{len(file_list)}] 📂 Processando: {s3_key}")
        
        try:
            # 1. Download
            s3.download_file(bucket, s3_key, local_path)
            
            # 2. Leitura
            df = pd.read_parquet(local_path)
            
            # 3. Verificar se a coluna já existe
            if 'ingestion_date' in df.columns:
                logging.warning(f"⚠️  [{idx+1}/{len(file_list)}] Coluna já existe. Pulando: {s3_key}")
                skipped_count += 1
                # LIMPEZA IMEDIATA se pular
                if os.path.exists(local_path):
                    os.remove(local_path)
                continue
            
            # 4. Adicionar coluna
            df['ingestion_date'] = ingestion_date
            
            # 5. Reescrever localmente
            df.to_parquet(local_path, index=False, engine='pyarrow')
            
            # 6. Upload (SOBRESCREVE o arquivo original)
            s3.upload_file(local_path, bucket, s3_key)
            
            # 7. LIMPEZA IMEDIATA após upload bem-sucedido
            if os.path.exists(local_path):
                os.remove(local_path)
                logging.debug(f"🗑️  Arquivo local deletado: {local_path}")
            
            success_count += 1
            logging.info(f"✅ [{idx+1}/{len(file_list)}] Sucesso: {s3_key}")
            
        except Exception as e:
            error_count += 1
            logging.error(f"❌ [{idx+1}/{len(file_list)}] FALHA em {s3_key}: {e}")
            
        finally:
            # 8. GARANTIA FINAL: Limpeza no finally (dupla segurança)
            if os.path.exists(local_path):
                try:
                    os.remove(local_path)
                    logging.debug(f"🗑️  [Finally] Arquivo local deletado: {local_path}")
                except Exception as cleanup_err:
                    logging.warning(f"⚠️  Erro na limpeza final de {local_path}: {cleanup_err}")
        
        # Checkpoint a cada 50 arquivos + Verificação de espaço
        if (idx + 1) % 50 == 0:
            logging.info(f"📊 Checkpoint: {success_count} sucessos | {skipped_count} pulados | {error_count} erros")
            
            # Verificar espaço em disco
            import shutil
            disk = shutil.disk_usage('/')
            free_gb = disk.free / (1024**3)
            logging.info(f"💾 Espaço livre em disco: {free_gb:.1f}GB")
            
            if free_gb < 10:
                logging.error(f"❌ CRÍTICO: Apenas {free_gb:.1f}GB livres. Abortando para segurança.")
                # Limpeza de emergência
                cleanup_all_temp_files()
                raise Exception("Espaço em disco insuficiente")
    
    logging.info("="*60)
    logging.info(f"📊 RESUMO FINAL:")
    logging.info(f"   ✅ Sucessos: {success_count}")
    logging.info(f"   ⚠️  Pulados: {skipped_count}")
    logging.info(f"   ❌ Erros: {error_count}")
    logging.info("="*60)

if __name__ == "__main__":
    logging.info("="*60)
    logging.info(f"🛠️  Manutenção Retroativa: Ingestion Date")
    logging.info(f"📅 Data de Ingestão: {INGESTION_DATE}")
    logging.info(f"🎯 Bucket: {TARGET_BUCKET}")
    logging.info(f"📁 Prefixo: {TARGET_PREFIX}")
    logging.info("="*60)
    
    # 0. LIMPEZA PREVENTIVA: Remove qualquer arquivo temporário antigo
    cleanup_all_temp_files()
    
    # 1. Listar todos os arquivos
    all_files = list_all_parquet_files(TARGET_BUCKET, TARGET_PREFIX)
    
    if not all_files:
        logging.warning("⚠️  Nenhum arquivo encontrado. Finalizando.")
        exit(0)
    
    # 2. Confirmação de segurança
    print(f"\n⚠️  ATENÇÃO: {len(all_files)} arquivos serão MODIFICADOS e SOBRESCRITOS!")
    print(f"📅 Coluna 'ingestion_date' será adicionada com valor: {INGESTION_DATE}")
    print("\n🔒 Esta operação é IRREVERSÍVEL sem backup!")
    confirm = input("\nDigite 'CONFIRMAR' para prosseguir (ou Enter para cancelar): ")
    
    if confirm != "CONFIRMAR":
        logging.info("❌ Operação cancelada pelo usuário.")
        exit(0)
    
    # 3. Processar e sobrescrever
    logging.info("🚀 Iniciando processamento...")
    try:
        process_and_overwrite(all_files, TARGET_BUCKET, INGESTION_DATE)
    except Exception as e:
        logging.error(f"❌ Erro durante processamento: {e}")
        cleanup_all_temp_files()  # Limpeza de emergência
        exit(1)
    finally:
        # 4. LIMPEZA FINAL GARANTIDA (mesmo se houver erro)
        cleanup_all_temp_files()
    
    logging.info("="*60)
    logging.info("🎉 Manutenção Retroativa CONCLUÍDA")
    logging.info("="*60)