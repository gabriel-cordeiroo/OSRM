# 📘 DOCUMENTAÇÃO OSRM DISTANCE PIPELINE

## SEÇÃO 6: MONITORAMENTO & NOTIFICAÇÕES

---

## 📊 SISTEMA DE OBSERVABILIDADE

Esta seção documenta o sistema completo de logs, alertas e notificações automáticas.

---

## 1. ARQUITETURA DE MONITORAMENTO

### 1.1 Fluxo Completo

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. EXECUÇÃO DO PIPELINE                                             │
│    osrm_run.sh                                                      │
│    └─ Salva logs localmente: osrm_automation.log                   │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ 2. UPLOAD DE LOG PARA S3                                            │
│    aws s3 cp osrm_automation.log                                    │
│                                                                      │
│    ┌─────────────────────────────────────────────┐                 │
│    │ s3://20-ze-datalake-landing/                │                 │
│    │                                             │                 │
│    │ osrm_success/  ◄─── Sucesso                │                 │
│    │ ├── 2025-12-01_040000_success.log          │                 │
│    │ └── 2025-12-02_040000_success.log          │                 │
│    │                                             │                 │
│    │ osrm_failed/   ◄─── Falha                  │                 │
│    │ ├── _osrm_timeout.log                      │                 │
│    │ ├── _container_restart.log                 │                 │
│    │ ├── _dedupe_failed.log                     │                 │
│    │ └── _pipeline_failed.log                   │                 │
│    └─────────────────────────────────────────────┘                 │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               │ (S3 Event: ObjectCreated)
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ 3. LAMBDA DETECTA NOVO LOG                                          │
│    osrm-log-monitor.lambda_handler()                                │
│    • Baixa log do S3                                                │
│    • Extrai estatísticas (parse de texto)                           │
│    • Identifica tipo (success/failure)                              │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ 4. FORMATAÇÃO DE MENSAGEM                                           │
│    format_success_message() OU format_failure_message()             │
│    • Cria mensagem estruturada                                      │
│    • Adiciona estatísticas relevantes                               │
│    • Define subject apropriado                                      │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ 5. PUBLICAÇÃO NO SNS                                                 │
│    sns_client.publish(TopicArn=..., Subject=..., Message=...)      │
└──────────────┬──────────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────────┐
│ 6. ENTREGA DE E-MAIL                                                 │
│    Para: br184733@ambev.com.br                                      │
│    Subject: ✅ OSRM Pipeline - Sucesso (2025-12-01)                │
│    Body: [Mensagem formatada]                                       │
│                                                                      │
│    Latência total: ~30-60 segundos                                  │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. ESTRUTURA DE LOGS

### 2.1 Formato de Logs

**Padrão:**
```
YYYY-MM-DD HH:MM:SS - [EMOJI] MENSAGEM
```

**Exemplos:**
```log
2025-12-01 04:00:05 - --- INICIANDO PIPELINE VIA @REBOOT ---
2025-12-01 04:00:06 - ✅ Ambiente virtual ativado com sucesso.
2025-12-01 04:00:06 - ⏳ Aguardando o servidor OSRM na porta 5000...
2025-12-01 04:00:15 - ✅ Servidor OSRM está pronto! Começando o processamento.
2025-12-01 04:00:15 - 🚀 Executando pipeline principal (osrm-request.py)...
2025-12-01 04:00:16 - 📅 Partição 1/2: 2025-12 (INCREMENTAL - mês corrente)
2025-12-01 04:00:17 - 📊 Total de registros no arquivo: 6,176,922
2025-12-01 04:15:28 - ✅ SUCESSO: Pipeline principal concluído.
2025-12-01 04:15:28 - 🧹 Iniciando dedupe do mês corrente...
2025-12-01 04:15:35 - ✅ Dedupe concluído com sucesso.
2025-12-01 04:15:35 - 🎉 Pipeline completo finalizado.
2025-12-01 04:15:36 - 📤 Log de sucesso enviado para S3
2025-12-01 04:15:36 - --- FIM DA EXECUÇÃO AUTOMÁTICA ---
```

### 2.2 Tipos de Arquivos de Log

**Estrutura de Nomes:**
```
{EXECUTION_DATE}_{EXECUTION_TIMESTAMP}_{STATUS_TYPE}.log

Componentes:
  • EXECUTION_DATE: YYYY-MM-DD
  • EXECUTION_TIMESTAMP: YYYYMMDD_HHMMSS
  • STATUS_TYPE:
    - success
    - osrm_timeout
    - container_restart
    - dedupe_failed
    - pipeline_failed
```

**Exemplos:**
```
2025-12-01_040000_success.log
2025-11-15_040000_osrm_timeout.log
2025-11-20_040000_container_restart.log
2025-11-25_040000_dedupe_failed.log
2025-11-28_040000_pipeline_failed.log
```

---

## 3. DETECÇÃO DE FALHAS

### 3.1 Health Check do OSRM

**Código (osrm_run.sh):**
```bash
MAX_TRIES=60  # 5 minutos de timeout (60 × 5s)
TRY=0

while [ $TRY -lt $MAX_TRIES ]; do
    # Testa se OSRM responde
    if curl -s http://localhost:5000/status > /dev/null; then
        log "✅ Servidor OSRM está pronto!"
        break
    fi
    
    # DETECTA: Container em loop de restart (mapa ausente)
    CONTAINER_STATUS=$(sudo docker inspect -f '{{.State.Status}}' osrm_server)
    if [ "$CONTAINER_STATUS" = "restarting" ]; then
        log "❌ AVISO CRÍTICO: O container OSRM está em estado 'restarting'. Abortando."
        
        # Salva log de falha
        aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_container_restart.log"
        exit 1
    fi
    
    sleep 5
    TRY=$((TRY+1))
done

# TIMEOUT: OSRM não respondeu após 5 minutos
if [ $TRY -eq $MAX_TRIES ]; then
    log "❌ TIMEOUT: Servidor OSRM não respondeu após $MAX_TRIES tentativas."
    aws s3 cp $LOG_FILE "s3://.../osrm_failed/${DATE}_${TIME}_osrm_timeout.log"
    exit 1
fi
```

**Falhas Detectadas:**

**1. Container Restart (Mapa Ausente)**
```log
2025-11-20 04:00:30 - ⏳ Aguardando o servidor OSRM na porta 5000...
2025-11-20 04:00:55 - ❌ AVISO CRÍTICO: O container OSRM está em estado 'restarting'. Abortando.
2025-11-20 04:00:56 - 📤 Log de falha enviado para S3

Causa: Mapa Brasil ausente ou corrompido em /data/brazil-latest.osrm
Ação: Recriar mapa com osrm-extract e osrm-contract
```

**2. OSRM Timeout (Servidor Não Responde)**
```log
2025-11-15 04:00:30 - ⏳ Aguardando o servidor OSRM na porta 5000...
2025-11-15 04:05:30 - ❌ TIMEOUT: Servidor OSRM não respondeu após 60 tentativas.
2025-11-15 04:05:31 - 📤 Log de falha enviado para S3

Causa: Container não subiu (Docker travado) ou porta ocupada
Ação: Verificar logs Docker, reiniciar serviço
```

### 3.2 Falhas no Pipeline

**3. Pipeline Failed (Erro no Python)**
```log
2025-11-28 04:10:15 - 🚀 Executando pipeline principal (osrm-request.py)...
2025-11-28 04:10:20 - ❌ FALHA: Pipeline principal falhou. Código de saída: 1.
2025-11-28 04:10:21 - 📤 Log de falha enviado para S3

Causa: Erro Python (S3 inacessível, falta de memória, bug)
Ação: Verificar log completo, analisar traceback
```

**4. Dedupe Failed (Erro na Consolidação)**
```log
2025-11-25 04:15:30 - 🧹 Iniciando dedupe do mês corrente...
2025-11-25 04:15:45 - ⚠️ AVISO: Dedupe falhou com código 1.
2025-11-25 04:15:46 - 📤 Log de falha (dedupe) enviado para S3

Causa: Arquivo corrompido, falta de espaço em disco
Ação: Verificar integridade dos arquivos, limpar disco
```

---

## 4. LAMBDA MONITOR (Processamento de Logs)

### 4.1 Extração de Estatísticas

**Função: extract_log_stats()**

```python
def extract_log_stats(log_content: str) -> dict:
    lines = log_content.split('\n')
    
    stats = {
        'total_processed': 0,
        'partitions': [],
        'duration': 'N/A',
        'errors': [],
        'warnings': [],
        'start_time': None,
        'end_time': None
    }
    
    for line in lines:
        # Total processado
        if 'Total processado:' in line:
            match = re.search(r'Total processado:\s*([\d,]+)', line)
            if match:
                stats['total_processed'] = match.group(1)
        
        # Partições processadas
        if 'Partição' in line and 'concluída' in line:
            match = re.search(r'Partição ([0-9\-]+) concluída', line)
            if match:
                stats['partitions'].append(match.group(1))
        
        # Erros
        if '❌' in line or 'ERROR' in line or 'FALHA' in line:
            stats['errors'].append(line.strip())
        
        # Avisos
        if '⚠️' in line or 'WARNING' in line:
            stats['warnings'].append(line.strip())
    
    # Extrai timestamps do início e fim
    if lines:
        first_line = lines[0]
        last_line = lines[-1]
        
        try:
            stats['start_time'] = first_line.split(' - ')[0].strip()
            stats['end_time'] = last_line.split(' - ')[0].strip()
        except:
            pass
    
    return stats
```

**Exemplo de Output:**
```json
{
  "total_processed": "6,176,922",
  "partitions": ["2025-12"],
  "errors": [],
  "warnings": [
    "2025-12-01 04:10:15 - ⚠️ Removidas 45.123 duplicatas do arquivo fonte!"
  ],
  "start_time": "2025-12-01 04:00:05",
  "end_time": "2025-12-01 04:15:36"
}
```

### 4.2 Formatação de Mensagens

**SUCESSO:**
```python
def format_success_message(filename: str, stats: dict) -> str:
    execution_date = filename.split('_')[0]
    execution_time = filename.split('_')[1]
    
    partitions_str = ', '.join(stats['partitions'][:5])
    if len(stats['partitions']) > 5:
        partitions_str += f" (e mais {len(stats['partitions']) - 5})"
    
    message = f"""
🎉 OSRM Pipeline - SUCESSO

📅 Data: {execution_date}
⏰ Horário: {execution_time.replace('_', ':')}

📊 ESTATÍSTICAS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Total processado: {stats['total_processed']} registros
• Partições: {len(stats['partitions'])} processadas
  └─ {partitions_str}

⏱️ TEMPO:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Início: {stats['start_time']}
• Fim: {stats['end_time']}

✅ Pipeline executado com sucesso!
"""
    
    if stats['warnings']:
        message += f"\n⚠️ AVISOS ({len(stats['warnings'])}):\n"
        for warning in stats['warnings'][:3]:
            message += f"  • {warning[:100]}\n"
        if len(stats['warnings']) > 3:
            message += f"  ... e mais {len(stats['warnings']) - 3} avisos\n"
    
    return message
```

**FALHA:**
```python
def format_failure_message(filename: str, stats: dict) -> str:
    execution_date = filename.split('_')[0]
    execution_time = filename.split('_')[1]
    failure_type = filename.split('_')[-1].replace('.log', '')
    
    failure_types = {
        'container_restart': 'Container OSRM reiniciando',
        'osrm_timeout': 'Timeout no servidor OSRM',
        'dedupe_failed': 'Falha no dedupe',
        'pipeline_failed': 'Falha no pipeline principal'
    }
    
    failure_description = failure_types.get(failure_type, 'Falha desconhecida')
    
    message = f"""
❌ OSRM Pipeline - FALHA

📅 Data: {execution_date}
⏰ Horário: {execution_time.replace('_', ':')}

🔴 TIPO DE FALHA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{failure_description}

📊 ESTATÍSTICAS PARCIAIS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Total processado: {stats['total_processed']} registros
• Partições processadas: {len(stats['partitions'])}

❌ ERROS ENCONTRADOS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    if stats['errors']:
        for error in stats['errors'][:5]:
            message += f"  • {error[:150]}\n"
        if len(stats['errors']) > 5:
            message += f"\n  ... e mais {len(stats['errors']) - 5} erros\n"
    else:
        message += "  (Verificar log completo no S3)\n"
    
    message += f"""
⚠️ AÇÃO NECESSÁRIA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Verificar log completo no S3
2. Analisar causa raiz da falha
3. Corrigir problema antes da próxima execução
"""
    
    return message
```

---

## 5. EXEMPLOS DE E-MAILS

### 5.1 E-mail de Sucesso (Exemplo Real)

```
De: AWS Notifications <no-reply@sns.amazonaws.com>
Para: br184733@ambev.com.br
Assunto: ✅ OSRM Pipeline - Sucesso (2025-12-01)
Data: 01/12/2025 04:16:30

🎉 OSRM Pipeline - SUCESSO

📅 Data: 2025-12-01
⏰ Horário: 04:00:00

📊 ESTATÍSTICAS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Total processado: 6,176,922 registros
• Partições: 1 processadas
  └─ 2025-12

⏱️ TEMPO:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Início: 2025-12-01 04:00:05
• Fim: 2025-12-01 04:15:36
• Duração: ~15 minutos

✅ Pipeline executado com sucesso!

⚠️ AVISOS (1):
  • 2025-12-01 04:10:15 - ⚠️ Removidas 45.123 duplicatas do arquivo fonte!
```

### 5.2 E-mail de Falha - Container Restart

```
De: AWS Notifications <no-reply@sns.amazonaws.com>
Para: br184733@ambev.com.br
Assunto: ❌ OSRM Pipeline - FALHA (2025-11-20)
Data: 20/11/2025 04:01:00

❌ OSRM Pipeline - FALHA

📅 Data: 2025-11-20
⏰ Horário: 04:00:30

🔴 TIPO DE FALHA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Container OSRM reiniciando

📊 ESTATÍSTICAS PARCIAIS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• Total processado: 0 registros
• Partições processadas: 0

❌ ERROS ENCONTRADOS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • ❌ AVISO CRÍTICO: O container OSRM está em estado 'restarting'. Abortando.

⚠️ AÇÃO NECESSÁRIA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Verificar log completo no S3
2. Analisar causa raiz da falha
3. Corrigir problema antes da próxima execução

🔗 Log completo:
s3://20-ze-datalake-landing/osrm_distance/osrm_failed/2025-11-20_040000_container_restart.log
```

### 5.3 E-mail de Falha - OSRM Timeout

```
De: AWS Notifications <no-reply@sns.amazonaws.com>
Para: br184733@ambev.com.br
Assunto: ❌ OSRM Pipeline - FALHA (2025-11-15)
Data: 15/11/2025 04:06:00

❌ OSRM Pipeline - FALHA

📅 Data: 2025-11-15
⏰ Horário: 04:00:30

🔴 TIPO DE FALHA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Timeout no servidor OSRM

❌ ERROS ENCONTRADOS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • ❌ TIMEOUT: Servidor OSRM não respondeu após 60 tentativas.

⚠️ AÇÃO NECESSÁRIA:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Conectar na VM via SSH
2. Verificar status do Docker: sudo docker ps -a
3. Verificar logs OSRM: sudo docker logs osrm_server
4. Reiniciar container se necessário
```

---

## 6. TROUBLESHOOTING

### 6.1 Verificação Manual de Logs

**Download do log:**
```bash
# Último log de sucesso
aws s3 cp s3://20-ze-datalake-landing/osrm_distance/osrm_success/ . --recursive

# Último log de falha
aws s3 cp s3://20-ze-datalake-landing/osrm_distance/osrm_failed/ . --recursive

# Visualizar
cat 2025-12-01_040000_success.log
```

**Buscar erros específicos:**
```bash
# Procurar por erros
grep "❌" 2025-12-01_040000_success.log

# Procurar por avisos
grep "⚠️" 2025-12-01_040000_success.log

# Procurar por estatísticas
grep "Total processado" 2025-12-01_040000_success.log
```

### 6.2 Teste de Notificações

**Teste manual:**
```bash
# Criar log de teste
echo "2025-12-01 04:00:00 - 🎉 Pipeline concluído
Total processado: 1,000,000 registros" > test.log

# Upload para disparar Lambda
aws s3 cp test.log s3://20-ze-datalake-landing/osrm_distance/osrm_success/2025-12-01_test_success.log

# Verificar CloudWatch Logs da Lambda
aws logs tail /aws/lambda/osrm-log-monitor --follow
```

**Verificar SNS:**
```bash
# Listar tópicos
aws sns list-topics | grep osrm

# Listar assinaturas
aws sns list-subscriptions | grep br184733

# Testar publicação
aws sns publish \
  --topic-arn arn:aws:sns:us-west-2:ACCOUNT:osrm-pipeline-notifications \
  --subject "Teste Manual" \
  --message "Teste de notificação manual"
```

---

## 7. PRÓXIMA SEÇÃO

**Seção 7:** Resultados & Métricas (performance final, ROI, lições aprendidas)

---

**Última Atualização:** 01/12/2025  
**Versão:** 1.0
