"""
Consolida e deduplica meses HISTÓRICOS (já fechados).
Roda APENAS UMA VEZ para cada mês passado.
"""

import boto3
import pandas as pd
import logging
from datetime import datetime
import os
import shutil

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BUCKET = "20-ze-datalake-landing"
BASE_PREFIX = "osrm_distance/osrm_landing"

# MESES HISTÓRICOS (já fechados, não vão receber dados novos)
HISTORICAL_MONTHS = [
    "2025-01", "2025-02", "2025-03", "2025-04", 
    "2025-05", "2025-06", "2025-07", "2025-08", 
    "2025-09", "2025-11"
]

def check_disk_space():
    disk = shutil.disk_usage('/')
    free_gb = disk.free / (1024**3)
    logging.info(f"💾 Espaço livre: {free_gb:.1f}GB")
    return free_gb > 10

def consolidate_month(bucket: str, year_month: str):
    """Consolida todos os arquivos de um mês em 1 único arquivo deduplicado."""
    
    year, month = year_month.split('-')
    prefix = f"{BASE_PREFIX}/year={year}/month={month}/"
    
    logging.info("="*60)
    logging.info(f"📅 Processando mês: {year_month}")
    logging.info("="*60)
    
    s3 = boto3.client('s3')
    
    # 1. Lista TODOS os arquivos do mês
    paginator = s3.get_paginator('list_objects_v2')
    files = []
    
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if 'Contents' in page:
            for obj in page['Contents']:
                if obj['Key'].endswith('.parquet'):
                    files.append(obj['Key'])
    
    if not files:
        logging.warning(f"⚠️  Nenhum arquivo encontrado em {prefix}")
        return
    
    logging.info(f"📂 Encontrados {len(files)} arquivo(s)")
    
    # 2. Baixa e concatena TODOS os arquivos
    dfs = []
    total_size_mb = 0
    
    for idx, file_key in enumerate(files):
        local_file = f"temp_{idx}.parquet"
        
        try:
            s3.download_file(bucket, file_key, local_file)
            file_size_mb = os.path.getsize(local_file) / (1024**2)
            total_size_mb += file_size_mb
            
            df = pd.read_parquet(local_file)
            dfs.append(df)
            
            os.remove(local_file)
            logging.info(f"✅ [{idx+1}/{len(files)}] Lido: {os.path.basename(file_key)} ({len(df):,} registros, {file_size_mb:.1f}MB)")
            
        except Exception as e:
            logging.error(f"❌ Erro ao processar {file_key}: {e}")
            if os.path.exists(local_file):
                os.remove(local_file)
            continue
    
    if not dfs:
        logging.error(f"❌ Nenhum arquivo foi lido com sucesso para {year_month}")
        return
    
    # 3. Concatena tudo
    logging.info(f"🔗 Concatenando {len(dfs)} DataFrames...")
    df_full = pd.concat(dfs, ignore_index=True)
    total_records_before = len(df_full)
    logging.info(f"📊 Total de registros ANTES do dedupe: {total_records_before:,}")
    
    # 4. Remove duplicatas
    logging.info(f"🧹 Removendo duplicatas por order_number...")
    df_dedupe = df_full.drop_duplicates(subset=['order_number'], keep='first')
    total_records_after = len(df_dedupe)
    duplicates_removed = total_records_before - total_records_after
    
    logging.info(f"📊 Total de registros APÓS dedupe: {total_records_after:,}")
    logging.info(f"🗑️  Duplicatas removidas: {duplicates_removed:,} ({duplicates_removed/total_records_before*100:.2f}%)")
    
    # 5. Salva arquivo consolidado
    consolidated_filename = f"consolidated_{year_month}.parquet"
    df_dedupe.to_parquet(consolidated_filename, index=False, engine='pyarrow')
    
    file_size_mb = os.path.getsize(consolidated_filename) / (1024**2)
    logging.info(f"💾 Arquivo consolidado criado: {file_size_mb:.1f}MB")
    
    # 6. Upload do arquivo consolidado
    consolidated_key = f"{prefix}consolidated-{year}-{month}.parquet"
    s3.upload_file(consolidated_filename, bucket, consolidated_key)
    logging.info(f"✅ Upload: s3://{bucket}/{consolidated_key}")
    
    os.remove(consolidated_filename)
    
    # 7. DELETE arquivos antigos (CUIDADO!)
    logging.warning(f"🗑️  DELETANDO {len(files)} arquivo(s) antigo(s)...")
    
    for file_key in files:
        try:
            s3.delete_object(Bucket=bucket, Key=file_key)
            logging.debug(f"   Deletado: {os.path.basename(file_key)}")
        except Exception as e:
            logging.error(f"   Erro ao deletar {file_key}: {e}")
    
    logging.info(f"✅ Mês {year_month} consolidado com sucesso!")
    logging.info(f"   Arquivos: {len(files)} → 1")
    logging.info(f"   Registros: {total_records_before:,} → {total_records_after:,}")
    logging.info(f"   Tamanho: {total_size_mb:.1f}MB → {file_size_mb:.1f}MB")

if __name__ == "__main__":
    logging.info("="*60)
    logging.info("🛠️  CONSOLIDAÇÃO E DEDUPE - MESES HISTÓRICOS")
    logging.info("="*60)
    
    if not check_disk_space():
        logging.error("❌ Espaço em disco insuficiente!")
        exit(1)
    
    print(f"\n⚠️  ATENÇÃO: Este script irá:")
    print(f"   1. Consolidar {len(HISTORICAL_MONTHS)} meses históricos")
    print(f"   2. Remover duplicatas por order_number")
    print(f"   3. DELETAR arquivos originais (irreversível!)")
    print(f"\n🔒 Meses: {', '.join(HISTORICAL_MONTHS)}")
    
    confirm = input("\nDigite 'CONFIRMAR' para prosseguir: ")
    
    if confirm != "CONFIRMAR":
        logging.info("❌ Cancelado pelo usuário.")
        exit(0)
    
    for month in HISTORICAL_MONTHS:
        try:
            consolidate_month(BUCKET, month)
            
            if not check_disk_space():
                logging.error("❌ Espaço insuficiente. Abortando.")
                break
                
        except Exception as e:
            logging.error(f"❌ ERRO ao processar {month}: {e}")
            continue
    
    logging.info("="*60)
    logging.info("🎉 CONSOLIDAÇÃO CONCLUÍDA")
    logging.info("="*60)