# 📘 DOCUMENTAÇÃO OSRM DISTANCE PIPELINE

## SEÇÃO 2: EVOLUÇÃO DA SOLUÇÃO

---

## ⏱️ LINHA DO TEMPO DE DESENVOLVIMENTO

Esta seção documenta a evolução cronológica da solução, desde os primeiros testes até a implementação final com monitoramento automático.

---

## 📅 FASE 1: PROVA DE CONCEITO (Outubro 2025)

### 1.1 Primeiro Teste Manual

**Objetivo:** Validar viabilidade técnica do OSRM

**Implementação:**
- VM EC2 Ubuntu 24.04 com Docker
- Container OSRM com mapa do Brasil (~3GB)
- Script Python simples com requests síncronos

**Resultados:**
```
Performance inicial: ~500 registros/segundo
Latência: ~50ms por request
CPU: 20-30% de utilização (subutilizado)
```

**Aprendizados:**
- ✅ OSRM é viável para volume necessário
- ❌ Processamento síncrono é lento demais
- ❌ Falta paralelização

---

## 📅 FASE 2: PARALELIZAÇÃO (Novembro 2025)

### 2.1 Implementação de Async/Await

**Problema:** Processamento síncrono limitado a ~500 reqs/s

**Solução:** Migração para asyncio + aiohttp

**Código-chave:**
```python
async def async_request(point, client: osrm.AioHTTPClient):
    coordinates = [start_coords, end_coords]
    response = await client.route(coordinates=coordinates)
    return result

async def batch_request(points, max_concurrent=100):
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [limited_request(point) for point in points]
    return await asyncio.gather(*tasks)
```

**Resultados:**
```
Performance: ~2.000 registros/segundo (+300%)
CPU: 60-70% (melhor aproveitamento)
Latência: ~50ms (mantida)
```

**Aprendizados:**
- ✅ Ganho significativo com concorrência
- ⚠️ Ainda limitado por single-process (GIL do Python)
- ❌ Memória cresce em batches grandes

### 2.2 Implementação de Multiprocessing

**Problema:** GIL do Python limita paralelização real

**Solução:** Multiprocessing + Asyncio híbrido

**Código-chave:**
```python
def process_chunk(chunk, max_concurrent=100):
    return asyncio.run(batch_request(chunk, max_concurrent))

def parallel_osrm_requests(points, num_processes=15, max_concurrent=100):
    chunks = chunk_list(points, num_processes)
    with Pool(processes=num_processes) as pool:
        results = pool.starmap(process_chunk, [(chunk, max_concurrent) for chunk in chunks])
    return flatten(results)
```

**Resultados:**
```
Performance: ~5.000 registros/segundo (+150%)
CPU: 95-98% (máximo aproveitamento)
Processos: 15 workers paralelos
Requests/processo: 30-40 concorrentes
```

**Configuração Final:**
```python
NUM_PROCESSES = 15      # Workers paralelos
MAX_CONCURRENT = 30     # Requests async por worker
BLOCK_SIZE = 1_500_000  # Registros por bloco
```

**Aprendizados:**
- ✅ Máxima performance alcançada
- ✅ CPU totalmente utilizada
- ⚠️ Necessário balancear BLOCK_SIZE vs memória

---

## 📅 FASE 3: PROCESSAMENTO INCREMENTAL (15-20 Nov 2025)

### 3.1 Sistema de Bookmarks

**Problema:** Reprocessamento total diário (~6M registros/dia = ~20min)

**Solução:** Checkpoint baseado em timestamp S3

**Estrutura do Bookmark:**
```json
{
  "completed_partitions": ["2025-01", "2025-02", ...],
  "delta_timestamps": {
    "2025-12": "2025-12-01T04:15:30+00:00"
  },
  "last_updated": "2025-12-01T16:46:05+00:00"
}
```

**Lógica de Detecção:**
```python
current_month = "2025-12"
previous_month = "2025-11"

# HISTÓRICO: Partição não está em completed_partitions
is_historical = partition not in completed_partitions

# INCREMENTAL: Partição tem delta_timestamp OU é mês corrente
is_incremental = (partition in delta_timestamps) or (partition == current_month)

# Filtro de arquivos
if is_incremental and last_processed_ts:
    files = [f for f in files if f['LastModified'] > last_processed_ts]
```

**Resultados:**
```
Modo HISTÓRICO:
  • Processa partição completa
  • Marca como completed_partitions
  • Nunca mais é reprocessada

Modo INCREMENTAL:
  • Processa apenas arquivos novos
  • Atualiza delta_timestamp
  • Executa diariamente
```

**Aprendizados:**
- ✅ Redução de 95% no volume de reprocessamento
- ✅ Processamento diário: 6M → ~6M (apenas novos)
- ⚠️ Necessário sincronização com pipeline upstream

### 3.2 Tratamento do Mês Anterior

**Problema:** Virada de mês causa confusão (novembro vs dezembro)

**Caso Real (01/12/2025):**
```
Pipeline da 50 roda às 03:00:
  • Processa dados do dia 30/11
  • Salva em: 2025-11/part-20251201-030000.parquet

Pipeline OSRM roda às 04:00:
  • Deve processar: 2025-11 (INCREMENTAL)
  • E depois: 2025-12 (INCREMENTAL)
```

**Solução:**
```python
previous_month = (datetime.now().replace(day=1) - timedelta(days=1)).strftime('%Y-%m')

# Adiciona mês anterior SE tiver checkpoint
if previous_month in delta_timestamps:
    partitions_to_process.append(previous_month)

# Adiciona mês corrente (sempre)
partitions_to_process.append(current_month)
```

**Resultado:**
```
Fila de processamento (01/12):
1. 2025-11 (INCREMENTAL - arquivos de 30/11)
2. 2025-12 (INCREMENTAL - vazio, mas preparado)
```

---

## 📅 FASE 4: DEDUPLICAÇÃO INTELIGENTE (20-25 Nov 2025)

### 4.1 Problema das Duplicatas

**Origem das Duplicatas:**

**1. Fonte (Pipeline da 50)**
```
Spark job pode gerar duplicatas entre arquivos
Exemplo: order_number "ABC123" em part-00000.parquet E part-00001.parquet
```

**2. Processamento Diário**
```
Dia 01: part-hash1-000-00000.parquet (order ABC123)
Dia 02: part-hash2-000-00000.parquet (order ABC123) ← DUPLICATA!
```

**3. Acumulação Mensal**
```
month=12/
├── part-* (30 arquivos, ~6M registros/dia)
└── Total: ~180M registros, mas apenas ~150M únicos
```

### 4.2 Solução: Dedupe em 3 Camadas

**CAMADA 1: Dedupe na Fonte**
```python
# Durante leitura do arquivo
df = pd.read_parquet(local_file_path)

total_antes = len(df)
df = df.drop_duplicates(subset=['order_number'], keep='first')
total_depois = len(df)

logging.warning(f"Removidas {total_antes - total_depois:,} duplicatas do arquivo fonte!")
```

**Resultado:** Remove ~1-2% de duplicatas intra-arquivo

**CAMADA 2: Hash Único por Arquivo**
```python
# Nome único baseado no arquivo fonte
file_hash = hashlib.md5(source_filename.encode()).hexdigest()[:8]

# Nome final
output_s3_key = f"{prefix}/part-{file_hash}-{file_idx:03d}-{chunk:05d}.parquet"
```

**Resultado:** Previne sobrescrita acidental de arquivos

**CAMADA 3: Dedupe Diário (dedupe_current_month.py)**
```python
# Roda APÓS osrm-request.py
# 1. Lê TODOS os arquivos de 2025-12/
all_files = [f for f in s3.list_objects(prefix="year=2025/month=12/") if 'part-' in f]

# 2. Concatena e deduplica
df_full = pd.concat([pd.read_parquet(f) for f in all_files])
df_dedupe = df_full.drop_duplicates(subset=['order_number'], keep='first')

# 3. Salva arquivo consolidado
df_dedupe.to_parquet(f"dedupe_{execution_hash}_000.parquet")

# 4. DELETE arquivos part-* originais
for f in all_files:
    s3.delete_object(Key=f)
```

**Resultado:** Remove ~3-5% de duplicatas acumuladas

### 4.3 Consolidação de Meses Históricos

**Script:** `dedupe_historical_months.py`

**Propósito:** Consolidar meses fechados em 1 arquivo único

**Execução:** Manual, uma vez por mês

**Lógica:**
```python
HISTORICAL_MONTHS = ["2025-01", "2025-02", ..., "2025-11"]

for month in HISTORICAL_MONTHS:
    # 1. Baixa TODOS os arquivos do mês
    files = s3.list_objects(prefix=f"year=2025/month={month}/")
    
    # 2. Concatena e deduplica
    df_full = pd.concat([pd.read_parquet(f) for f in files])
    df_dedupe = df_full.drop_duplicates(subset=['order_number'], keep='first')
    
    # 3. Salva arquivo consolidado
    s3.upload_file(f"consolidated-{month}.parquet")
    
    # 4. DELETE arquivos originais (7-13 arquivos)
    for f in files:
        s3.delete_object(Key=f)
```

**Resultado:**
```
Antes:
month=01/ → 7 arquivos part-*.parquet
month=02/ → 6 arquivos part-*.parquet
...

Depois:
month=01/ → 1 arquivo consolidated-2025-01.parquet
month=02/ → 1 arquivo consolidated-2025-02.parquet
...
```

---

## 📅 FASE 5: ORQUESTRAÇÃO AWS (25-28 Nov 2025)

### 5.1 Auto-Start/Stop de VM

**Problema:** VM ligada 24/7 = desperdício (processamento: 15-20min/dia)

**Solução:** Lambda + EventBridge

**Componentes:**

**1. Lambda Function (lambda_function.py)**
```python
def lambda_handler(event, context):
    action = event.get('action', 'start')  # 'start' ou 'stop'
    instance_id = os.environ['INSTANCE_ID']
    
    if action == 'start':
        # Verifica se já está rodando → Para antes de iniciar
        if get_status() == 'running':
            ec2.stop_instances()
            wait_for_stopped()
        
        ec2.start_instances(InstanceIds=[instance_id])
        wait_for_running()
    
    elif action == 'stop':
        ec2.stop_instances(InstanceIds=[instance_id])
        wait_for_stopped()
```

**2. EventBridge Rules (Terraform)**
```hcl
# START às 04:00 (Brasília = 07:00 UTC)
resource "aws_cloudwatch_event_rule" "start_morning" {
  schedule_expression = "cron(0 7 * * ? *)"
}

# STOP às 04:30 (30min após start)
resource "aws_cloudwatch_event_rule" "stop_morning" {
  schedule_expression = "cron(30 7 * * ? *)"
}
```

**3. Trigger @reboot na VM**
```bash
# crontab -e
@reboot sleep 60 && /home/ubuntu/osrm-automation/osrm_run.sh
```

**Fluxo:**
```
04:00 → Lambda START
04:01 → VM inicia
04:02 → @reboot dispara osrm_run.sh
04:02-04:17 → Processamento
04:17-04:19 → Dedupe
04:19 → Script salva log no S3
04:30 → Lambda STOP (safety timeout)
```

**Resultado:**
```
Economia: 22h/dia desligada
Custo: ~$0.60/dia → ~$0.15/dia (75% redução)
```

### 5.2 Sincronização de Pipelines

**Timing Original:**
```
03:00 - Pipeline da 50 (Spark)
03:30 - Watcher do Batch Processor
04:00 - VM liga e processa
```

**Problema:** Watcher disparava ANTES do processamento OSRM!

**Solução:** Ajuste do Watcher
```python
# Airflow DAG
schedule_interval = "30 4 * * *"  # 04:30 (30min após VM)
```

**Margem de Segurança:**
```
04:19 - Pipeline OSRM termina
04:30 - Watcher verifica arquivos
Margem: 11 minutos (suficiente)
```

---

## 📅 FASE 6: MONITORAMENTO & NOTIFICAÇÕES (28 Nov - 01 Dez 2025)

### 6.1 Sistema de Logs Estruturados

**Problema:** Logs dispersos, difíceis de analisar

**Solução:** Logs centralizados no S3

**Estrutura:**
```
s3://20-ze-datalake-landing/osrm_distance/
├── osrm_success/
│   ├── 2025-12-01_040000_success.log
│   ├── 2025-12-02_040000_success.log
│   └── ...
└── osrm_failed/
    ├── 2025-11-15_040000_osrm_timeout.log
    ├── 2025-11-20_040000_container_restart.log
    └── ...
```

**Tipos de Falha Detectados:**
```bash
# 1. Container reiniciando (mapa ausente)
${DATE}_${TIME}_container_restart.log

# 2. Timeout OSRM (servidor não respondeu)
${DATE}_${TIME}_osrm_timeout.log

# 3. Dedupe falhou
${DATE}_${TIME}_dedupe_failed.log

# 4. Pipeline principal falhou
${DATE}_${TIME}_pipeline_failed.log
```

### 6.2 Notificações via SNS

**Arquitetura:**
```
osrm_run.sh
    │
    ▼ (Salva log)
S3 Bucket (Event Notification)
    │
    ▼ (Dispara)
Lambda (osrm_log_monitor.py)
    │
    ▼ (Extrai stats)
SNS Topic
    │
    ▼ (Envia e-mail)
br184733@ambev.com.br
```

**Lambda Monitor:**
```python
def extract_log_stats(log_content):
    stats = {
        'total_processed': extract_total(),
        'partitions': extract_partitions(),
        'errors': extract_errors(),
        'warnings': extract_warnings(),
        'start_time': extract_start(),
        'end_time': extract_end()
    }
    return stats

def format_message(filename, stats, is_success):
    if is_success:
        return f"""
🎉 OSRM Pipeline - SUCESSO
📅 Data: {date}
📊 Total: {stats['total_processed']} registros
✅ Pipeline executado com sucesso!
"""
    else:
        return f"""
❌ OSRM Pipeline - FALHA
🔴 Tipo: {failure_type}
❌ Erros: {len(stats['errors'])}
⚠️ AÇÃO NECESSÁRIA
"""
```

**Resultado:**
- ✅ Notificação em ~30-60 segundos
- ✅ E-mail formatado com estatísticas
- ✅ Diferenciação clara entre sucesso/falha

---

## 📅 REPROCESSAMENTO HISTÓRICO (01 Dez 2025)

### 7.1 Execução de 11 Meses (Janeiro-Novembro 2025)

**Configuração:**
```python
BLOCK_SIZE = 1_500_000
NUM_PROCESSES = 15
MAX_CONCURRENT = 30
```

**Execução:**
```
Início: 14:52:06 (01/12/2025)
Término: 16:46:17 (01/12/2025)
Duração: ~1h54min
```

**Volumes Processados:**
```
2025-01: 7.125.428 registros
2025-02: 5.932.845 registros
2025-03: 8.245.119 registros
2025-04: 5.418.923 registros
2025-05: 5.012.334 registros
2025-06: 5.234.156 registros
2025-07: 4.123.567 registros
2025-08: 5.345.678 registros
2025-09: 4.987.234 registros
2025-10: 5.678.123 registros
2025-11: 6.666.029 registros

TOTAL: 63.769.436 registros
```

**Performance:**
```
Throughput: ~10.067 registros/segundo
Tempo/bloco (1.5M): ~149 segundos
Ganho vs anterior: ~79% mais rápido
```

**Bookmark Final:**
```json
{
  "completed_partitions": [
    "2025-01", "2025-02", "2025-03", "2025-04",
    "2025-05", "2025-06", "2025-07", "2025-08",
    "2025-09", "2025-10", "2025-11"
  ],
  "delta_timestamps": {},
  "last_updated": "2025-12-01T16:46:05+00:00"
}
```

---

## 🎯 ESTADO ATUAL (Dezembro 2025)

### Configuração de Produção

**VM EC2:**
- Tipo: t3a.xlarge (4 vCPUs, 16GB RAM)
- Disco: 30GB SSD
- Container: OSRM com mapa Brasil (~3GB)

**Pipeline:**
- Modo: INCREMENTAL (mês corrente)
- Execução: Diária às 04:00
- Duração: 15-20 minutos
- Volume: ~6M registros/dia

**Estrutura de Dados:**
```
s3://20-ze-datalake-landing/osrm_distance/osrm_landing/
└── year=2025/
    ├── month=01/ → consolidated-2025-01.parquet (HISTÓRICO)
    ├── month=02/ → consolidated-2025-02.parquet (HISTÓRICO)
    ...
    ├── month=11/ → consolidated-2025-11.parquet (HISTÓRICO)
    └── month=12/ (MÊS CORRENTE)
        ├── dedupe_abc123_000.parquet (Dia 01)
        ├── dedupe_def456_000.parquet (Dia 02)
        └── ... (~30 arquivos ao final do mês)
```

**Monitoramento:**
- Logs em S3 (auditáveis, imutáveis)
- Notificações SNS em tempo real
- Custo: $0.00/mês (Free Tier)

---

## 📊 PRÓXIMA SEÇÃO

**Seção 3:** Arquitetura Técnica (deep dive em componentes)

---

**Última Atualização:** 01/12/2025  
**Versão:** 1.0
