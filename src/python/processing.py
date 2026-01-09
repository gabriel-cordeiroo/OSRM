# processing.py - CÓDIGO FINAL E CORRIGIDO

import osrm
import logging
import asyncio
import timeit
import pandas as pd
import numpy as np
import os
import subprocess
import requests
import json
import warnings
from multiprocessing import Pool, cpu_count
from contextlib import contextmanager

from config import SETUP, StartEndPair 

# --- OSRM E REQUISIÇÕES PARALELAS ---

@contextmanager
def suppress_warnings():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield

def parse_df(df):
    """Filtra e converte colunas de coordenadas para float."""
    with suppress_warnings():
        df = df[SETUP["metadata_columns"] + SETUP["start_coordinates"] + SETUP["end_coordinates"]]
        float_columns = SETUP["start_coordinates"] + SETUP["end_coordinates"]
        for c in float_columns:
            df[c] = df[c].astype(float)
        return df

def make_list_of_coords(df):
    """Converte DataFrame em lista de dicionários para processamento OSRM."""
    return df.to_dict(orient='records')


async def get_client() -> osrm.AioHTTPClient:
    """Cria e retorna o cliente assíncrono OSRM."""
    return osrm.AioHTTPClient(host='http://localhost:5000', max_retries=10, timeout=10)

async def async_request(point: dict, client: osrm.AioHTTPClient = None, max_retries: int = 5):
    """Faz uma única requisição assíncrona ao OSRM com retries."""
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
            if attempt < max_retries:
                await asyncio.sleep(0.1 * (2 ** attempt))
                if "disconnected" in str(e).lower(): 
                    client = await get_client()
                if "no route" in str(e).lower(): 
                    return None
            else:
                logging.error(f"Falha permanente após {max_retries} tentativas.")
                return None

async def batch_request(points: List[StartEndPair], max_concurrent = 100):
    """Gerencia requisições assíncronas em paralelo com limite de concorrência."""
    client = await get_client()
    semaphore = asyncio.Semaphore(max_concurrent)
    
    async def limited_request(point):
        async with semaphore: 
            return await async_request(point, client)
            
    tasks = [limited_request(point) for point in points]
    output = await asyncio.gather(*tasks)
    await client.close()
    
    return [x for x in output if x is not None]

def process_chunk(chunk: List[StartEndPair], max_concurrent = 100):
    """Função wrapper para rodar o asyncio dentro do Processo."""
    return asyncio.run(batch_request(chunk, max_concurrent=max_concurrent))

def chunk_list(lst, n):
    """Divide a lista em N pedaços para N processos."""
    k, m = divmod(len(lst), n)
    return [lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]

def parallel_osrm_requests(points: List[StartEndPair], num_processes=None, max_concurrent=100):
    """Orquestra as requisições paralelas usando Pool de processos."""
    if num_processes is None: num_processes = cpu_count()
    chunks = chunk_list(points, num_processes)
    
    with Pool(processes=num_processes) as pool:
        results_nested = pool.starmap(process_chunk, [(chunk, max_concurrent) for chunk in chunks])
        
    return [item for sublist in results_nested for item in sublist]

# --- VERIFICAÇÕES DE AMBIENTE ---

def check_disk_space():
    """Verifica espaço em disco e alerta se crítico."""
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

def shutdown_instance():
    """Auto-desliga a instância com tratamento robusto de erros."""
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
            logging.error(f"❌ Resposta inválida da API de metadados.")
            return
        
        logging.info(f"Desligando a instância {instance_id} na região {region}...")
        subprocess.run(
            ["aws", "ec2", "stop-instances", "--instance-ids", instance_id, "--region", region], 
            check=True,
            capture_output=True,
            text=True
        )
        logging.info("✅ Comando de desligamento executado.")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Erro de rede ao acessar metadados: {e}")
    except subprocess.CalledProcessError as e:
        logging.error(f"❌ Erro ao executar comando AWS CLI: {e.stderr}")
    except Exception as e:
        logging.error(f"❌ Erro inesperado no desligamento: {e}")