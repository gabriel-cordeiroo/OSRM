"""
Deduplica o mês CORRENTE sem consolidar em 1 arquivo.
Mantém múltiplos arquivos, mas garante uniqueness.
Roda DIARIAMENTE após o pipeline principal.
"""

import boto3
import pandas as pd
import logging
from datetime import datetime
import os
import hashlib

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BUCKET = "20-ze-datalake-landing"
BASE_PREFIX = "osrm_distance/osrm_landing"

def dedupe_current_month():
    """Lê TODOS os arquivos do mês corrente, remove duplicatas, reescreve."""
    
    current_month = datetime.now().strftime('%Y-%m')
    year, month = current_month.split('-')
    prefix = f"{BASE_PREFIX}/year={year}/month={month}/"
    
    logging.info("="*60)
    logging.info(f"📅 Deduplicando mês corrente: {current_month}")
    logging.info("="*60)
    
    s3 = boto3.client('s3')
    
    # 1. Lista arquivos
    paginator = s3.get_paginator('list_objects_v2')
    files = []
    
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                # Ignora arquivos já consolidados
                if key.endswith('.parquet') and 'consolidated' not in key:
                    files.append(key)
    
    if not files:
        logging.warning(f"⚠️  Nenhum arquivo encontrado")
        return
    
    logging.info(f"📂 Encontrados {len(files)} arquivo(s)")
    
    # 2. Lê TODOS os arquivos
    dfs = []
    for idx, file_key in enumerate(files):
        local_file = f"temp_{idx}.parquet"
        
        try:
            s3.download_file(BUCKET, file_key, local_file)
            df = pd.read_parquet(local_file)
            dfs.append(df)
            os.remove(local_file)
            
            logging.info(f"✅ [{idx+1}/{len(files)}] Lido: {os.path.basename(file_key)} ({len(df):,} registros)")
            
        except Exception as e:
            logging.error(f"❌ Erro: {e}")
            if os.path.exists(local_file):
                os.remove(local_file)
            continue
    
    # 3. Concatena e deduplica
    df_full = pd.concat(dfs, ignore_index=True)
    total_before = len(df_full)
    
    logging.info(f"📊 Total ANTES: {total_before:,}")
    
    df_dedupe = df_full.drop_duplicates(subset=['order_number'], keep='first')
    total_after = len(df_dedupe)
    
    logging.info(f"📊 Total APÓS: {total_after:,}")
    logging.info(f"🗑️  Removidas: {total_before - total_after:,} duplicatas")
    
    # 4. Divide em chunks para manter compatibilidade com DAG
    chunk_size = 1_000_000
    num_chunks = (len(df_dedupe) // chunk_size) + 1
    
    execution_hash = hashlib.md5(datetime.now().isoformat().encode()).hexdigest()[:8]
    
    logging.info(f"💾 Salvando {num_chunks} arquivo(s)...")
    
    new_files = []
    for i in range(num_chunks):
        start = i * chunk_size
        end = start + chunk_size
        chunk = df_dedupe[start:end]
        
        if len(chunk) == 0:
            continue
        
        # Nome com hash único + timestamp
        filename = f"dedupe_{execution_hash}_{i:03d}.parquet"
        local_path = filename
        s3_key = f"{prefix}{filename}"
        
        chunk.to_parquet(local_path, index=False)
        s3.upload_file(local_path, BUCKET, s3_key)
        os.remove(local_path)
        
        new_files.append(s3_key)
        logging.info(f"   ✅ Salvo: {filename} ({len(chunk):,} registros)")
    
    # 5. DELETE arquivos antigos
    logging.warning(f"🗑️  Deletando {len(files)} arquivo(s) antigo(s)...")
    
    for file_key in files:
        try:
            s3.delete_object(Bucket=BUCKET, Key=file_key)
        except Exception as e:
            logging.error(f"Erro ao deletar {file_key}: {e}")
    
    logging.info(f"✅ Dedupe concluído!")
    logging.info(f"   Arquivos: {len(files)} → {len(new_files)}")
    logging.info(f"   Registros: {total_before:,} → {total_after:,}")

if __name__ == "__main__":
    dedupe_current_month()