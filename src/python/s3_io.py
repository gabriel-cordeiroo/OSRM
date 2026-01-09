# s3_io.py - CÓDIGO FINAL E CORRIGIDO

import boto3
import json
import logging
import os
import shutil
from datetime import datetime, timedelta
from typing import List, Dict
from pytz import timezone
from botocore.exceptions import ClientError as BotoClientError


# --- BOOKMARKS E METADADOS ---

def list_s3_partitions(bucket, prefix):
    """Lista partições no formato YYYY-MM na source."""
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    partitions = set()
    if not prefix.endswith('/'): prefix += '/'
    logging.info(f"Listando partições em s3://{bucket}/{prefix}")
    try:
        for result in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
            for common_prefix in result.get('CommonPrefixes', []):
                partition_name = common_prefix.get('Prefix').split('/')[-2]
                if len(partition_name) == 7 and partition_name[4] == '-':
                    partitions.add(partition_name)
        return sorted(list(partitions))
    except Exception as e:
        logging.error(f"Erro ao listar partições S3: {e}")
        return []

def get_processed_bookmark(bucket, key) -> Dict:
    """Lê bookmark com histórico e deltas."""
    s3 = boto3.client('s3')
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        bookmark = json.loads(obj['Body'].read().decode('utf-8'))
        return bookmark
    except BotoClientError as e:
        if e.response['Error']['Code'] == 'NoSuchKey':
            logging.warning(f"Arquivo de bookmark não encontrado. Criando novo.")
            return {"completed_partitions": [], "delta_timestamps": {}}
        raise
    except Exception as e:
        logging.error(f"Erro ao ler bookmark: {e}")
        return {"completed_partitions": [], "delta_timestamps": {}}

def update_processed_bookmark(bucket, key, completed_partition: str = None, delta_timestamp: str = None, partition_name: str = None):
    """Atualiza bookmark com validação."""
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
    
    bookmark["last_updated"] = datetime.now(timezone('UTC')).isoformat()
    
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(bookmark, indent=2))
        logging.info(f"✅ Bookmark atualizado em s3://{bucket}/{key}")
    except Exception as e:
        logging.error(f"❌ CRÍTICO: Falha ao salvar bookmark: {e}")
        raise

# --- ARQUIVOS S3 I/O ---

def list_s3_objects(bucket, prefix=''):
    """Lista objetos S3 com metadados LastModified."""
    s3 = boto3.client('s3')
    paginator = s3.get_paginator('list_objects_v2')
    page_iterator = paginator.paginate(Bucket=bucket, Prefix=prefix)
    
    files = []
    for page in page_iterator:
        if 'Contents' in page:
            for obj in page['Contents']:
                files.append({'Key': obj['Key'], 'LastModified': obj['LastModified'].isoformat()})
    return files

def check_file_exists_s3(bucket: str, key: str) -> bool:
    """Verifica se arquivo existe no S3."""
    s3 = boto3.client('s3')
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except BotoClientError as e:
        if e.response['Error']['Code'] == '404':
            return False
        raise

def upload_file_to_s3(file_path, bucket_name, s3_key):
    """Upload com tratamento de nome duplicado (Fallback)."""
    s3 = boto3.client('s3')
    
    if check_file_exists_s3(bucket_name, s3_key):
        logging.error(f"❌ CRÍTICO: Arquivo já existe em s3://{bucket_name}/{s3_key}")
        # Gerar nome alternativo (segurança)
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

def delete_s3_prefix(bucket, prefix):
    """Deleta objetos com prefixo específico."""
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
                        objects_to_delete = []
        if objects_to_delete:
            s3.delete_objects(Bucket=bucket, Delete={'Objects': objects_to_delete})
        logging.info(f"✅ EXCLUSÃO CONCLUÍDA de s3://{bucket}/{prefix}")
    except Exception as e:
        logging.error(f"Erro ao excluir objetos do S3: {e}")
        raise

def load_existing_order_numbers(bucket: str, prefix: str) -> set:
    """
    Carrega todos os order_numbers já existentes na partição do S3.
    
    Args:
        bucket: Nome do bucket
        prefix: Prefixo da partição (ex: osrm_distance/osrm_landing/year=2025/month=12/)
    
    Returns:
        Set com todos os order_numbers já processados
    """
    s3 = boto3.client('s3')
    existing_orders = set()
    
    logging.info(f"🔍 Carregando order_numbers existentes de s3://{bucket}/{prefix}")
    
    # Lista arquivos existentes (dedupe-* e consolidated-*)
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    
    if 'Contents' not in response:
        logging.info("✅ Nenhum arquivo existente (primeira execução da partição)")
        return existing_orders
    
    files_found = [obj['Key'] for obj in response['Contents'] if obj['Key'].endswith('.parquet')]
    
    if not files_found:
        logging.info("✅ Nenhum arquivo parquet existente")
        return existing_orders
    
    logging.info(f"📋 Encontrados {len(files_found)} arquivo(s) existente(s) para verificar")
    
    # Ler apenas a coluna order_number de cada arquivo
    for file_key in files_found:
        try:
            # Download temporário
            local_temp = f"/tmp/{os.path.basename(file_key)}"
            s3.download_file(bucket, file_key, local_temp)
            
            # Ler apenas order_number (otimizado)
            import pandas as pd
            df_existing = pd.read_parquet(local_temp, columns=['order_number'])
            existing_orders.update(df_existing['order_number'].tolist())
            
            # Limpar
            os.remove(local_temp)
            
            logging.info(f"  ✓ Lido: {os.path.basename(file_key)} ({len(df_existing):,} orders)")
            
        except Exception as e:
            logging.warning(f"⚠️  Erro ao ler {file_key}: {e}")
            continue
    
    logging.info(f"✅ Total de order_numbers existentes carregados: {len(existing_orders):,}")
    
    return existing_orders