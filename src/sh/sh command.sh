#!/bin/bash

# --- 1. CONFIGURAÇÕES & LOGS ---
# Local onde este script está sendo executado.
cd "$(dirname "$0")"

LOG_FILE="osrm_automation.log"
CONTAINER_NAME="osrm_server"
MAP_DIR="${HOME}/osrm-brazil-files"
# S3 para backup dos arquivos do OSRM (a fonte para a restauração)
MAP_S3_PATH="s3://20-ze-datalake-landing/territory_osrm/osrm-brazil-files/"
DOCKER_RUN_COMMAND="sudo docker run -d --restart always --name ${CONTAINER_NAME} -p 5000:5000 -v ${MAP_DIR}:/data ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm mld /data/brazil-latest.osrm"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a $LOG_FILE
}

log "--- INICIANDO EXECUÇÃO AUTOMÁTICA DO PIPELINE OSRM ---"

# --- 2. ATIVAÇÃO DO VIRTUAL ENVIRONMENT ---
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
    log "Ambiente virtual ativado com sucesso."
else
    log "ERRO: O arquivo .venv/bin/activate não foi encontrado. Abortando."
    exit 1
fi

# --- 3. FUNÇÃO DE RECUPERAÇÃO DO MAPA E DOCKER ---
recover_docker_and_maps() {
    log "Iniciando recuperação: Parando, removendo e restaurando arquivos de mapa."
    
    # Parar e Remover o container problemático
    sudo docker stop $CONTAINER_NAME 2>/dev/null
    sudo docker rm $CONTAINER_NAME 2>/dev/null

    # Restaurar arquivos do S3
    log "Baixando arquivos OSRM do S3 para ${MAP_DIR}..."
    mkdir -p "${MAP_DIR}" # Garante que o diretório exista
    aws s3 cp $MAP_S3_PATH $MAP_DIR --recursive
    
    if [ $? -ne 0 ]; then
        log "ERRO: Falha ao baixar arquivos do S3. Verifique as permissões IAM. Abortando."
        exit 1
    fi
    log "Download concluído."

    # Iniciar novo container
    log "Iniciando o container Docker novamente."
    $DOCKER_RUN_COMMAND
    if [ $? -ne 0 ]; then
        log "ERRO: Falha ao executar o comando Docker. Abortando."
        exit 1
    fi
}

# --- 4. VERIFICAÇÃO E INÍCIO DO CONTAINER ---

# Verifica o status do container
CONTAINER_STATUS=$(sudo docker inspect -f '{{.State.Status}}' $CONTAINER_NAME 2>/dev/null)

if [ -z "$CONTAINER_STATUS" ]; then
    log "Container ${CONTAINER_NAME} não encontrado ou inativo. Tentando iniciar/recuperar."
    # Se não existe, executa a lógica de recuperação para garantir os arquivos e o container
    recover_docker_and_maps
elif [ "$CONTAINER_STATUS" = "restarting" ]; then
    log "Container em status 'restarting'. Presumindo arquivos ausentes/corrompidos. Iniciando recuperação..."
    recover_docker_and_maps
elif [ "$CONTAINER_STATUS" = "exited" ]; then
    log "Container ${CONTAINER_NAME} está 'exited'. Iniciando o container."
    sudo docker start $CONTAINER_NAME
else
    log "Container ${CONTAINER_NAME} está em status '$CONTAINER_STATUS'. Prosseguindo."
fi

# --- 5. ESPERA ATÉ O OSRM ESTAR PRONTO (Health Check) ---
log "Aguardando o servidor OSRM na porta 5000..."
MAX_TRIES=60 # Tenta por até 5 minutos (60 * 5s)
TRY=0

while [ $TRY -lt $MAX_TRIES ]; do
    # Health check: tenta conectar à porta 5000
    if curl -s http://localhost:5000/status > /dev/null; then
        log "Servidor OSRM está pronto!"
        break
    fi
    
    # Verifica se o container não voltou ao status de erro durante a espera
    NEW_STATUS=$(sudo docker inspect -f '{{.State.Status}}' $CONTAINER_NAME 2>/dev/null)
    if [ "$NEW_STATUS" = "restarting" ]; then
         log "AVISO: O container voltou ao estado 'restarting'. Abortando."
         exit 1
    fi
    
    sleep 5
    TRY=$((TRY+1))
done

if [ $TRY -eq $MAX_TRIES ]; then
    log "ERRO: Servidor OSRM não respondeu após 5 minutos. Abortando a execução do Python."
    exit 1
fi

# --- 6. EXECUÇÃO DO SCRIPT PYTHON ---
log "Iniciando script Python (osrm-request.py)..."
python osrm-request.py
EXIT_CODE=$?

# --- 7. FINALIZAÇÃO ---
deactivate # Desativa o venv (boa prática)

if [ $EXIT_CODE -eq 0 ]; then
    log "Pipeline OSRM concluído com SUCESSO. A VM será desligada pelo script Python."
else
    log "ERRO: O script python falhou. Código de saída: $EXIT_CODE."
fi

log "--- FIM DA EXECUÇÃO AUTOMÁTICA ---"
exit $EXIT_CODE