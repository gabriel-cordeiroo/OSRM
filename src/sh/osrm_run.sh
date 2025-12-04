#!/bin/bash
cd "$(dirname "$0")"

LOG_FILE="osrm_automation.log"
CONTAINER_NAME="osrm_server"
EXECUTION_DATE=$(date '+%Y-%m-%d')
EXECUTION_TIMESTAMP=$(date '+%Y%m%d_%H%M%S')

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a $LOG_FILE; }

log "--- INICIANDO PIPELINE VIA @REBOOT ---"

# 1. Ativação do ambiente Python
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
    log "✅ Ambiente virtual ativado com sucesso."
else
    log "⚠️  Ambiente virtual não encontrado."
fi

# 2. Espera OBRIGATÓRIA (Health Check)
log "⏳ Aguardando o servidor OSRM na porta 5000..."
MAX_TRIES=60
TRY=0

while [ $TRY -lt $MAX_TRIES ]; do
    if curl -s http://localhost:5000/status > /dev/null; then
        log "✅ Servidor OSRM está pronto! Começando o processamento."
        break
    fi
    
    # Checagem contra o erro de mapa ausente
    CONTAINER_STATUS=$(sudo docker inspect -f '{{.State.Status}}' $CONTAINER_NAME 2>/dev/null)
    if [ "$CONTAINER_STATUS" = "restarting" ]; then
         log "❌ AVISO CRÍTICO: O container OSRM está em estado 'restarting'. Abortando."
         
         # SALVAR LOG DE FALHA NO S3
         aws s3 cp $LOG_FILE "s3://20-ze-datalake-landing/osrm_distance/osrm_failed/${EXECUTION_DATE}_${EXECUTION_TIMESTAMP}_container_restart.log"
         log "📤 Log de falha enviado para S3"
         
         exit 1
    fi
    
    sleep 5
    TRY=$((TRY+1))
done

if [ $TRY -eq $MAX_TRIES ]; then
    log "❌ TIMEOUT: Servidor OSRM não respondeu após $MAX_TRIES tentativas."
    
    # SALVAR LOG DE FALHA NO S3
    aws s3 cp $LOG_FILE "s3://20-ze-datalake-landing/osrm_distance/osrm_failed/${EXECUTION_DATE}_${EXECUTION_TIMESTAMP}_osrm_timeout.log"
    log "📤 Log de falha enviado para S3"
    
    exit 1
fi

# 3. Execução do Pipeline Principal
log "🚀 Executando pipeline principal (osrm-request.py)..."
python osrm-request.py
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log "✅ SUCESSO: Pipeline principal concluído."
    
    # 4. Aguarda propagação S3 (10 segundos)
    log "⏳ Aguardando 10 segundos para propagação S3/SQS/DynamoDB..."
    sleep 10
    
    # 5. Executa Dedupe do Mês Corrente
    log "🧹 Iniciando dedupe do mês corrente..."
    
    if [ -f "dedupe_current_month.py" ]; then
        # Redireciona TODA a saída do dedupe para o log principal
        python dedupe_current_month.py 2>&1 | while IFS= read -r line; do
            clean_line=$(echo "$line" | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2},[0-9]{3} - (INFO|WARNING|ERROR|DEBUG) - //')
            echo "$(date '+%Y-%m-%d %H:%M:%S') - $clean_line" | tee -a $LOG_FILE
        done
        
        DEDUPE_EXIT=${PIPESTATUS[0]}
        
        if [ $DEDUPE_EXIT -eq 0 ]; then
            log "✅ Dedupe concluído com sucesso."
        else
            log "⚠️  AVISO: Dedupe falhou com código $DEDUPE_EXIT."
            
            # SALVAR LOG DE FALHA NO S3
            aws s3 cp $LOG_FILE "s3://20-ze-datalake-landing/osrm_distance/osrm_failed/${EXECUTION_DATE}_${EXECUTION_TIMESTAMP}_dedupe_failed.log"
            log "📤 Log de falha (dedupe) enviado para S3"
            
            exit 1
        fi
    else
        log "⚠️  AVISO: Arquivo dedupe_current_month.py não encontrado. Pulando dedupe."
    fi
    
    # 6. Pipeline completo - SALVAR LOG DE SUCESSO
    log "🎉 Pipeline completo finalizado."
    
    # SALVAR LOG DE SUCESSO NO S3
    aws s3 cp $LOG_FILE "s3://20-ze-datalake-landing/osrm_distance/osrm_success/${EXECUTION_DATE}_${EXECUTION_TIMESTAMP}_success.log"
    log "📤 Log de sucesso enviado para S3"
    
    log "🔌 Lambda irá desligar a VM automaticamente."
else
    log "❌ FALHA: Pipeline principal falhou. Código de saída: $EXIT_CODE."
    
    # SALVAR LOG DE FALHA NO S3
    aws s3 cp $LOG_FILE "s3://20-ze-datalake-landing/osrm_distance/osrm_failed/${EXECUTION_DATE}_${EXECUTION_TIMESTAMP}_pipeline_failed.log"
    log "📤 Log de falha enviado para S3"
    
    log "⚠️  VM não será desligada automaticamente devido à falha."
fi

# Desativa ambiente virtual se foi ativado
if [ -n "$VIRTUAL_ENV" ]; then
    deactivate
fi

log "--- FIM DA EXECUÇÃO AUTOMÁTICA ---"

exit $EXIT_CODE