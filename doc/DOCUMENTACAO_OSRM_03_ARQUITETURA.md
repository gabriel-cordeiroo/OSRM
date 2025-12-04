# 📘 DOCUMENTAÇÃO OSRM DISTANCE PIPELINE

## SEÇÃO 3: ARQUITETURA TÉCNICA

---

## 🏗️ COMPONENTES DO SISTEMA

Esta seção detalha cada componente da arquitetura, suas responsabilidades, interfaces e decisões técnicas.

---

## 1. CAMADA DE ORQUESTRAÇÃO AWS

### 1.1 EventBridge (Scheduler)

**Responsabilidade:** Disparar inicialização e desligamento da VM em horários específicos

**Configuração Terraform:**
```hcl
# START às 04:00 (Brasília = 07:00 UTC)
resource "aws_cloudwatch_event_rule" "start_morning" {
  name                = "ec2-auto-start-morning"
  schedule_expression = "cron(0 7 * * ? *)"
  description         = "Iniciar EC2 às 4h (Brasília)"
}

# STOP às 04:30 (Safety timeout - 30min após start)
resource "aws_cloudwatch_event_rule" "stop_morning" {
  name                = "ec2-auto-stop-morning"
  schedule_expression = "cron(30 7 * * ? *)"
  description         = "Parar EC2 30min após iniciar"
}
```

**Target:**
```hcl
resource "aws_cloudwatch_event_target" "lambda_start" {
  rule      = aws_cloudwatch_event_rule.start_morning.name
  arn       = aws_lambda_function.ec2_scheduler.arn
  input     = jsonencode({ action = "start" })
}
```

**Decisões Técnicas:**
- ⚙️ **Cron UTC vs Local:** EventBridge usa UTC, conversão: Brasília = UTC-3
- ⚙️ **Safety Stop:** 30min é suficiente para processar 6M registros + margem
- ⚙️ **Duplicação de Schedules:** 2x/dia (04:00 e 12:00) para flexibilidade futura

### 1.2 Lambda de Controle (ec2-auto-start)

**Função:** `lambda_function.py`

**Responsabilidades:**
1. Iniciar VM EC2 em horário programado
2. Garantir estado consistente (para antes de ligar, se necessário)
3. Desligar VM após janela de execução
4. Validar status com Waiter (bloqueio até confirmação)

**Código-chave:**
```python
def lambda_handler(event, context):
    ec2 = boto3.client('ec2')
    action = event.get('action', 'start')
    instance_id = os.environ['INSTANCE_ID']
    
    if action == 'start':
        current_state = get_instance_status(ec2, instance_id)
        
        # CRÍTICO: Garantir que VM está parada antes de iniciar
        if current_state == 'running':
            logging.info("VM já rodando. Parando primeiro...")
            ec2.stop_instances(InstanceIds=[instance_id])
            waiter = ec2.get_waiter('instance_stopped')
            waiter.wait(InstanceIds=[instance_id])
        
        # Iniciar VM
        ec2.start_instances(InstanceIds=[instance_id])
        
        # AGUARDAR status 'running'
        waiter = ec2.get_waiter('instance_running')
        waiter.wait(InstanceIds=[instance_id])
        
        return {'statusCode': 200, 'message': 'VM iniciada'}
    
    elif action == 'stop':
        # Verificar se já está parada
        current_state = get_instance_status(ec2, instance_id)
        
        if current_state == 'stopped':
            return {'message': 'VM já está parada'}
        
        # Parar VM
        ec2.stop_instances(InstanceIds=[instance_id])
        
        # AGUARDAR status 'stopped'
        waiter = ec2.get_waiter('instance_stopped')
        waiter.wait(
            InstanceIds=[instance_id],
            WaiterConfig={'Delay': 15, 'MaxAttempts': 40}  # ~10 min timeout
        )
        
        return {'statusCode': 200, 'message': 'VM parada'}
```

**Decisões Técnicas:**
- ⚙️ **Waiter Pattern:** Garante que Lambda só retorna após confirmação de estado
- ⚙️ **Check-before-Start:** Previne erro de "instância já rodando"
- ⚙️ **Timeout Generoso:** 10 minutos para stop (casos de alto I/O)
- ⚙️ **Idempotência:** Pode ser chamado múltiplas vezes sem efeito colateral

**IAM Policy:**
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:DescribeInstances"
      ],
      "Resource": "*"
    }
  ]
}
```

---

## 2. CAMADA DE PROCESSAMENTO (VM EC2)

### 2.1 Instância EC2

**Especificações:**
```
Tipo: t3a.xlarge
vCPUs: 4
RAM: 16 GB
Disco: 30 GB SSD (gp3)
AMI: Ubuntu 24.04 LTS
Região: us-west-2
```

**Justificativa de Dimensionamento:**
- **4 vCPUs:** Suporta 15 processos paralelos (oversubscription controlado)
- **16 GB RAM:** 
  - OSRM: ~3-4 GB
  - Python workers: ~15 × 400MB = ~6 GB
  - Sistema: ~2 GB
  - Margem: ~4 GB
- **30 GB Disco:** 
  - SO: ~10 GB
  - OSRM map: ~3 GB
  - Docker: ~5 GB
  - Temporários: ~10 GB
  - Margem: ~2 GB

### 2.2 Container OSRM

**Imagem:** `ghcr.io/project-osrm/osrm-backend`

**Configuração:**
```bash
docker run -d \
  --name osrm_server \
  -p 5000:5000 \
  --restart=always \
  -v /home/ubuntu/osrm-data:/data \
  ghcr.io/project-osrm/osrm-backend \
  osrm-routed --algorithm mld /data/brazil-latest.osrm
```

**Parâmetros:**
- `--algorithm mld`: Multi-Level Dijkstra (rápido para muitas queries)
- `--restart=always`: Reinicia se travar (com limite de 10 tentativas)
- `-p 5000:5000`: Porta padrão OSRM

**Health Check:**
```bash
curl -s http://localhost:5000/status
```

**Resposta esperada:**
```json
{
  "status": "Ok"
}
```

### 2.3 Script de Inicialização (@reboot)

**Arquivo:** `osrm_run.sh`

**Trigger:**
```bash
# crontab -e
@reboot sleep 60 && /home/ubuntu/osrm-automation/osrm_run.sh
```

**Fluxo de Execução:**
```bash
#!/bin/bash

# 1. SETUP
LOG_FILE="osrm_automation.log"
EXECUTION_DATE=$(date '+%Y-%m-%d')
EXECUTION_TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

# 2. ATIVAR AMBIENTE PYTHON
source .venv/bin/activate

# 3. HEALTH CHECK (CRÍTICO!)
MAX_TRIES=60  # 5 minutos de timeout
TRY=0

while [ $TRY -lt $MAX_TRIES ]; do
    # Verifica se OSRM está respondendo
    if curl -s http://localhost:5000/status > /dev/null; then
        log "✅ OSRM pronto!"
        break
    fi
    
    # DETECTA ESTADO DE REINICIALIZAÇÃO (mapa ausente)
    CONTAINER_STATUS=$(sudo docker inspect -f '{{.State.Status}}' osrm_server)
    if [ "$CONTAINER_STATUS" = "restarting" ]; then
        log "❌ CRÍTICO: Container em loop de restart"
        
        # SALVAR LOG DE FALHA
        aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_container_restart.log"
        exit 1
    fi
    
    sleep 5
    TRY=$((TRY+1))
done

# TIMEOUT: OSRM não respondeu
if [ $TRY -eq $MAX_TRIES ]; then
    log "❌ TIMEOUT: OSRM não respondeu"
    aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_osrm_timeout.log"
    exit 1
fi

# 4. EXECUTAR PIPELINE PRINCIPAL
python osrm-request.py
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    log "❌ Pipeline falhou"
    aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_pipeline_failed.log"
    exit 1
fi

# 5. AGUARDAR PROPAGAÇÃO S3 (10 segundos)
sleep 10

# 6. EXECUTAR DEDUPE
python dedupe_current_month.py 2>&1 | while read line; do
    # Remove timestamp do Python (já temos do bash)
    clean_line=$(echo "$line" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} .+ - //')
    log "$clean_line"
done

DEDUPE_EXIT=${PIPESTATUS[0]}

if [ $DEDUPE_EXIT -ne 0 ]; then
    log "❌ Dedupe falhou"
    aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_dedupe_failed.log"
    exit 1
fi

# 7. SUCESSO - SALVAR LOG
log "🎉 Pipeline concluído!"
aws s3 cp $LOG_FILE "s3://.../osrm_success/${DATE}_${TIME}_success.log"

exit 0
```

**Decisões Técnicas:**
- ⚙️ **Sleep 60s antes do @reboot:** Garante que Docker/OSRM iniciaram
- ⚙️ **Health Check Robusto:** Detecta container em loop de restart
- ⚙️ **Logs Únicos:** Timestamp no nome previne sobrescrita
- ⚙️ **Propagação S3:** 10s de espera garante consistência eventual
- ⚙️ **Merge de Logs:** Dedupe escreve no mesmo arquivo que pipeline principal

---

## 3. PIPELINE PRINCIPAL (osrm-request.py)

### 3.1 Arquitetura do Script

**Estrutura Modular:**
```
osrm-request.py
├── SETUP (Configurações)
├── Funções Auxiliares
│   ├── Gestão de S3 (list, upload, delete)
│   ├── Gestão de Bookmark (read, update)
│   ├── Health Checks (disk, OSRM)
│   └── Utilidades (hash, parse_df, chunk_list)
├── OSRM Client
│   ├── async_request (single request)
│   ├── batch_request (async batch)
│   └── parallel_osrm_requests (multiprocessing)
└── Main Loop
    ├── Inicialização
    ├── Identificar Fila de Trabalho
    ├── Loop por Partição
    │   ├── Listar e Filtrar Arquivos
    │   ├── Loop por Arquivo
    │   │   ├── Download
    │   │   ├── Dedupe Fonte
    │   │   └── Loop por Bloco (1.5M registros)
    │   │       ├── Processamento OSRM
    │   │       └── Upload S3
    │   └── Atualizar Bookmark
    └── Shutdown Instance (se sem trabalho)
```

### 3.2 Configurações Críticas

```python
SETUP = {
    # S3
    "input_s3_base_prefix": 'data_mesh/vw_antifraud_fact_distances',
    "output_s3_base_prefix": 'osrm_distance/osrm_landing',
    "bookmark_s3_key": 'osrm_distance/control/bookmark.json',
    
    # Colunas
    "start_coordinates": ["poc_longitude", "poc_latitude"],
    "end_coordinates": ["order_longitude", "order_latitude"],
    "metadata_columns": ["order_number"],
    
    # Performance
    "BATCH_SIZE": 1024 * 40,      # Tamanho do batch async (não usado)
    "NUM_PROCESSES": 15,           # Workers paralelos
    "MAX_CONCURRENT": 30,          # Requests async por worker
    "BLOCK_SIZE": 1_500_000,       # Registros por bloco de processamento
    
    # Flags
    "skip_download": False,
}
```

**Balanceamento de Performance:**
```
Total de requests concorrentes = NUM_PROCESSES × MAX_CONCURRENT
                                = 15 × 30 = 450 requests simultâneas

CPU por processo: 100% / 15 = ~6.7% (oversubscription controlado)
Requests/segundo: ~450 / 0.05s (latência OSRM) = ~9.000 teórico
                  Na prática: ~5.000 (overhead, I/O)
```

### 3.3 Lógica de Detecção de Modo

**Código:**
```python
current_month_partition = datetime.now().strftime('%Y-%m')  # "2025-12"
previous_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')  # "2025-11"

available_partitions = list_s3_partitions(SOURCE_BUCKET, SETUP["input_s3_base_prefix"])
# Exemplo: ["2025-01", "2025-02", ..., "2025-11", "2025-12"]

full_bookmark = get_processed_bookmark(DESTINATION_BUCKET, SETUP["bookmark_s3_key"])
processed_partitions_history = set(full_bookmark.get("completed_partitions", []))
# Exemplo: ["2025-01", "2025-02", ..., "2025-10"]

delta_timestamps = full_bookmark.get("delta_timestamps", {})
# Exemplo: {"2025-11": "2025-12-01T03:15:00+00:00"}

# LÓGICA: Históricos = disponíveis - processados - mês corrente
historical_work = set(available_partitions) - processed_partitions_history - {current_month_partition}
# Resultado: ["2025-11"] (se não estiver em processed)

# SE mês anterior tem checkpoint, NÃO é histórico
if previous_month in delta_timestamps:
    historical_work.discard(previous_month)

partitions_to_process = sorted(list(historical_work))

# ADICIONA mês anterior SE tiver checkpoint (INCREMENTAL)
if previous_month in available_partitions and previous_month in delta_timestamps:
    partitions_to_process.append(previous_month)

# ADICIONA mês corrente (SEMPRE INCREMENTAL)
if current_month_partition in available_partitions:
    partitions_to_process.append(current_month_partition)

# Resultado final: ["2025-11", "2025-12"] em 01/12/2025
```

**Tipos de Job:**
```python
is_current_month = (partition == current_month_partition)
is_previous_month = (partition == previous_month)
has_checkpoint = (partition in delta_timestamps)

if is_current_month:
    job_type = "INCREMENTAL (mês corrente)"
elif is_previous_month and has_checkpoint:
    job_type = "INCREMENTAL (mês anterior com checkpoint)"
else:
    job_type = "HISTÓRICO"
```

### 3.4 Processamento OSRM (Multiprocessing + Async)

**Arquitetura Híbrida:**
```
Main Process
    │
    ├─▶ Worker 1 (Process) ──┬─▶ Async Request 1
    │                         ├─▶ Async Request 2
    │                         └─▶ ... (30 concorrentes)
    │
    ├─▶ Worker 2 (Process) ──┬─▶ Async Request 1
    │                         └─▶ ... (30 concorrentes)
    │
    └─▶ ... Worker 15
```

**Código:**
```python
# NÍVEL 1: Async Client (dentro de cada worker)
async def async_request(point, client: osrm.AioHTTPClient, max_retries=5):
    start_coords = [point['poc_longitude'], point['poc_latitude']]
    end_coords = [point['order_longitude'], point['order_latitude']]
    
    for attempt in range(1, max_retries + 1):
        try:
            response = await client.route(
                coordinates=[start_coords, end_coords],
                overview=osrm.overview.false
            )
            
            return {
                'order_number': point['order_number'],
                'distance': float(response['routes'][0]['distance']),
                'duration': float(response['routes'][0]['duration'])
            }
        
        except Exception as e:
            # RETRY com exponential backoff
            if attempt < max_retries:
                await asyncio.sleep(0.1 * (2 ** attempt))  # 0.2s, 0.4s, 0.8s, 1.6s
                
                # Reconectar client se "disconnected"
                if "disconnected" in str(e).lower():
                    client = await get_client()
                
                # Desistir se "no route"
                if "no route" in str(e).lower():
                    return None
            else:
                logging.error(f"Falha permanente após {max_retries} tentativas")
                return None

# NÍVEL 2: Batch Async (gerencia semáforo de concorrência)
async def batch_request(points: List, max_concurrent=100):
    client = await get_client()
    semaphore = asyncio.Semaphore(max_concurrent)  # Limita a 30/worker
    
    async def limited_request(point):
        async with semaphore:
            return await async_request(point, client)
    
    tasks = [limited_request(point) for point in points]
    results = await asyncio.gather(*tasks)
    
    await client.close()
    return [r for r in results if r is not None]

# NÍVEL 3: Process Worker (cada worker roda seu próprio event loop)
def process_chunk(chunk: List, max_concurrent=100):
    return asyncio.run(batch_request(chunk, max_concurrent=max_concurrent))

# NÍVEL 4: Multiprocessing Pool (divide trabalho entre workers)
def parallel_osrm_requests(points: List, num_processes=15, max_concurrent=30):
    # Divide pontos em N chunks (1 por worker)
    chunks = chunk_list(points, num_processes)
    
    # Cria pool de processos
    with Pool(processes=num_processes) as pool:
        results_nested = pool.starmap(
            process_chunk,
            [(chunk, max_concurrent) for chunk in chunks]
        )
    
    # Flatten results
    return [item for sublist in results_nested for item in sublist]
```

**Decisões Técnicas:**
- ⚙️ **Semaphore:** Previne sobrecarga do OSRM (30 requests/worker é ideal)
- ⚙️ **Exponential Backoff:** Retry inteligente sem DDoS no servidor
- ⚙️ **Client Reuse:** 1 client por worker (economia de conexões TCP)
- ⚙️ **Chunking Inteligente:** Divisão equitativa entre workers

---

## 4. DEDUPE DO MÊS CORRENTE

### 4.1 Script: dedupe_current_month.py

**Propósito:** Consolidar arquivos do mês corrente e remover duplicatas diariamente

**Fluxo:**
```
1. Detecta mês corrente (2025-12)
2. Lista TODOS os arquivos em year=2025/month=12/
3. Ignora arquivos "consolidated-*" ou "dedupe-*" já processados
4. Download de cada arquivo
5. Concatenação em memória
6. drop_duplicates(subset=['order_number'], keep='first')
7. Divide em chunks de 1M (compatibilidade com DAG)
8. Upload com hash único: dedupe_{hash}_{idx}.parquet
9. DELETE arquivos part-* originais
```

**Código-chave:**
```python
def dedupe_current_month():
    current_month = datetime.now().strftime('%Y-%m')  # "2025-12"
    year, month = current_month.split('-')
    prefix = f"{BASE_PREFIX}/year={year}/month={month}/"
    
    # 1. Listar arquivos
    files = []
    for page in s3.paginator('list_objects_v2', Bucket=BUCKET, Prefix=prefix):
        for obj in page.get('Contents', []):
            key = obj['Key']
            # Ignora consolidados
            if key.endswith('.parquet') and 'consolidated' not in key and 'dedupe' not in key:
                files.append(key)
    
    if not files:
        logging.warning("Nenhum arquivo para deduplicar")
        return
    
    # 2. Download e concatenação
    dfs = []
    for file_key in files:
        s3.download_file(BUCKET, file_key, "temp.parquet")
        df = pd.read_parquet("temp.parquet")
        dfs.append(df)
        os.remove("temp.parquet")
    
    df_full = pd.concat(dfs, ignore_index=True)
    total_before = len(df_full)
    
    # 3. Dedupe
    df_dedupe = df_full.drop_duplicates(subset=['order_number'], keep='first')
    total_after = len(df_dedupe)
    
    logging.info(f"Removidas {total_before - total_after:,} duplicatas")
    
    # 4. Dividir em chunks de 1M
    chunk_size = 1_000_000
    num_chunks = (len(df_dedupe) // chunk_size) + 1
    
    execution_hash = hashlib.md5(datetime.now().isoformat().encode()).hexdigest()[:8]
    
    new_files = []
    for i in range(num_chunks):
        chunk = df_dedupe[i*chunk_size:(i+1)*chunk_size]
        
        if len(chunk) == 0:
            continue
        
        # Nome: dedupe_{hash}_{idx}.parquet
        filename = f"dedupe_{execution_hash}_{i:03d}.parquet"
        s3_key = f"{prefix}{filename}"
        
        chunk.to_parquet(filename, index=False)
        s3.upload_file(filename, BUCKET, s3_key)
        os.remove(filename)
        
        new_files.append(s3_key)
    
    # 5. DELETE arquivos antigos
    for file_key in files:
        s3.delete_object(Bucket=BUCKET, Key=file_key)
    
    logging.info(f"Arquivos: {len(files)} → {len(new_files)}")
    logging.info(f"Registros: {total_before:,} → {total_after:,}")
```

**Decisões Técnicas:**
- ⚙️ **Hash no Nome:** Previne conflitos entre execuções do mesmo dia
- ⚙️ **Chunk Size 1M:** Compatibilidade com limites do Batch Processor
- ⚙️ **Delete After Upload:** Libera espaço imediatamente
- ⚙️ **Ignora Consolidados:** Permite reexecutar sem duplicar trabalho

---

## 5. SISTEMA DE BOOKMARK

### 5.1 Estrutura de Dados

**Arquivo:** `s3://20-ze-datalake-landing/osrm_distance/control/bookmark.json`

**Schema:**
```json
{
  "completed_partitions": [
    "2025-01",
    "2025-02",
    "2025-11"
  ],
  "delta_timestamps": {
    "2025-12": "2025-12-01T04:15:30+00:00"
  },
  "last_updated": "2025-12-01T16:46:05.049404+00:00"
}
```

**Campos:**
- `completed_partitions`: Meses HISTÓRICOS já processados (nunca reprocessar)
- `delta_timestamps`: Checkpoint do último arquivo processado por mês INCREMENTAL
- `last_updated`: Timestamp da última atualização (auditoria)

### 5.2 Lógica de Atualização

**Código:**
```python
def update_processed_bookmark(
    bucket, 
    key, 
    completed_partition: str = None,
    delta_timestamp: str = None,
    partition_name: str = None
):
    bookmark = get_processed_bookmark(bucket, key)
    
    # HISTÓRICO: Marca como completo
    if completed_partition:
        if completed_partition not in bookmark.get("completed_partitions", []):
            bookmark["completed_partitions"].append(completed_partition)
            bookmark["completed_partitions"].sort()
        
        # Remove checkpoint se existir
        bookmark["delta_timestamps"].pop(completed_partition, None)
    
    # INCREMENTAL: Atualiza timestamp
    if delta_timestamp and partition_name:
        current_ts = bookmark["delta_timestamps"].get(partition_name)
        
        # Atualiza APENAS se novo timestamp é MAIOR
        if not current_ts or delta_timestamp > current_ts:
            bookmark["delta_timestamps"][partition_name] = delta_timestamp
    
    # Adiciona timestamp de auditoria
    bookmark["last_updated"] = datetime.now(timezone('UTC')).isoformat()
    
    # Salva no S3
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(bookmark, indent=2)
    )
    
    # VERIFICAÇÃO CRÍTICA: Confirma que foi salvo
    verified = get_processed_bookmark(bucket, key)
    if delta_timestamp and partition_name:
        saved_ts = verified.get("delta_timestamps", {}).get(partition_name)
        if saved_ts != delta_timestamp:
            raise Exception("Falha na verificação do bookmark!")
    
    logging.info("✅ Bookmark verificado e confirmado")
```

**Decisões Técnicas:**
- ⚙️ **Verificação Pós-Save:** Garante consistência (previne S3 eventual consistency)
- ⚙️ **Comparação de Timestamps:** Previne regressão (não pode voltar no tempo)
- ⚙️ **Remoção de Checkpoint:** Quando mês vira histórico, limpa delta
- ⚙️ **Auditoria:** `last_updated` permite debug de problemas

---

## 6. GESTÃO DE NOMES DE ARQUIVO

### 6.1 Estratégia de Naming

**Problema:** Múltiplas execuções do mesmo arquivo fonte não devem sobrescrever

**Solução:** Hash baseado no nome do arquivo fonte

**Código:**
```python
def generate_file_hash(filename: str, length: int = 8) -> str:
    return hashlib.md5(filename.encode()).hexdigest()[:8]

# Exemplo:
source_filename = "part-00000-abc123.snappy.parquet"
file_hash = generate_file_hash(source_filename.replace('.parquet', ''))
# Resultado: "a1b2c3d4"

# Nome final no S3:
output_s3_key = f"{prefix}/part-{file_hash}-{file_idx:03d}-{chunk:05d}.parquet"
# Resultado: "part-a1b2c3d4-000-00000.parquet"
```

**Anatomia do Nome:**
```
part-{hash}-{file_idx}-{chunk}.parquet
│    │      │           │
│    │      │           └─ Índice do chunk (00000, 00001, ...)
│    │      └─ Índice do arquivo na fila (000, 001, ...)
│    └─ Hash MD5 do nome fonte (8 chars)
└─ Prefixo identificador
```

**Vantagens:**
- ✅ Reprocessamento seguro (não sobrescreve)
- ✅ Rastreabilidade (hash → arquivo fonte)
- ✅ Ordem cronológica (file_idx preserva sequência)

---

## 7. PRÓXIMA SEÇÃO

**Seção 4:** Lógica de Processamento (algoritmos, estratégias de dedupe, edge cases)

---

**Última Atualização:** 01/12/2025  
**Versão:** 1.0
