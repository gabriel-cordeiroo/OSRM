import boto3
import os
import json
from botocore.exceptions import ClientError

# --- FUNÇÃO HELPER PARA VERIFICAR O STATUS ---
def get_instance_status(ec2_client, instance_id):
    """
    Retorna o estado da instância ('running', 'stopped', etc.).
    """
    try:
        response = ec2_client.describe_instances(InstanceIds=[instance_id])
        state = response['Reservations'][0]['Instances'][0]['State']['Name']
        return state
    except ClientError as e:
        # Se a instância não for encontrada, retorna um status específico
        if 'InvalidInstanceID.NotFound' in str(e):
            return "not_found"
        raise

# --- HANDLER PRINCIPAL ---
def lambda_handler(event, context):
    ec2 = boto3.client('ec2')
    
    # Obtém o ID da variável de ambiente (obrigatório)
    instance_id = os.environ.get('INSTANCE_ID')
    
    # Obtém a ação ('start' é o padrão)
    action = event.get('action', 'start')
    
    # Verifica se o ID está configurado
    if not instance_id:
        print("ERRO: INSTANCE_ID não configurado")
        return {
            'statusCode': 400,
            'body': json.dumps({'error': 'INSTANCE_ID não configurado'})
        }
    
    try:
        if action == 'stop':
            # === ALTERAÇÃO 2: Verificar e Esperar o Stop ===
            
            # Garante que a instância está ou será desligada
            current_state = get_instance_status(ec2, instance_id)
            
            if current_state == 'stopped':
                print(f"Instância {instance_id} já está parada. Nenhuma ação necessária.")
            elif current_state == 'running':
                print(f"Instância {instance_id} está rodando. Tentando PARAR.")
                ec2.stop_instances(InstanceIds=[instance_id])
                
                # Espera até que a instância esteja parada para retornar 200
                waiter = ec2.get_waiter('instance_stopped')
                print("Aguardando status 'stopped' (máximo 10 minutos)...")
                waiter.wait(
                    InstanceIds=[instance_id],
                    WaiterConfig={'Delay': 15, 'MaxAttempts': 40} # ~10 minutos
                )
                print("Sucesso! Instância confirmada como parada.")
            else:
                 print(f"Instância {instance_id} em estado {current_state}. Tentando PARAR.")
                 ec2.stop_instances(InstanceIds=[instance_id])
                 # Espera e trata o estado final
                 waiter = ec2.get_waiter('instance_stopped')
                 waiter.wait(InstanceIds=[instance_id])


            return {
                'statusCode': 200,
                'body': json.dumps({'message': f'Instância {instance_id} parada e status verificado com sucesso.'})
            }
        
        else: # action == 'start'
            # === ALTERAÇÃO 1: Verificar e Parar antes de Iniciar ===
            current_state = get_instance_status(ec2, instance_id)

            if current_state == 'running':
                print(f"Instância {instance_id} já está rodando. Desligando antes de iniciar...")
                
                # Desliga a instância
                ec2.stop_instances(InstanceIds=[instance_id])
                waiter = ec2.get_waiter('instance_stopped')
                print("Aguardando status 'stopped' para prosseguir com o START...")
                waiter.wait(InstanceIds=[instance_id])
                
                print("Instância parada. Prosseguindo com o START.")
            
            elif current_state == 'pending':
                print(f"Instância {instance_id} em estado 'pending'. Aguardando status 'running'.")
                waiter = ec2.get_waiter('instance_running')
                waiter.wait(InstanceIds=[instance_id])
                return {
                    'statusCode': 200,
                    'body': json.dumps({'message': f'Instância {instance_id} já iniciada.'})
                }

            
            # Executa o START
            print(f"Tentando INICIAR instância: {instance_id}")
            response = ec2.start_instances(InstanceIds=[instance_id])
            
            # Opcional: Esperar até que esteja rodando (para evitar que o EventBridge dispare a próxima regra antes)
            waiter = ec2.get_waiter('instance_running')
            waiter.wait(InstanceIds=[instance_id])
            
            print(f"Sucesso! Instância {instance_id} iniciada e status verificado.")
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': f'Instância {instance_id} iniciada com sucesso e status verificado',
                    'startingInstances': response['StartingInstances']
                })
            }
            
    except Exception as e:
        error_msg = f"Erro ao executar ação '{action}' na instância {instance_id}: {str(e)}"
        print(error_msg)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': error_msg})
        }