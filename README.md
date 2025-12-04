# 📘 DOCUMENTAÇÃO OSRM DISTANCE PIPELINE

## SEÇÃO 1: VISÃO GERAL & CONTEXTO

---

## 🎯 RESUMO EXECUTIVO

Este documento descreve a solução completa desenvolvida para automatizar o cálculo de distâncias rodoviárias utilizando o serviço OSRM (Open Source Routing Machine), incluindo processamento incremental, deduplicação inteligente, orquestração de infraestrutura AWS e sistema de monitoramento automático.

**Resultado Final:** Pipeline totalmente automatizado, resiliente e escalável, processando ~6M de registros/dia com zero intervenção manual.

---

## 📊 PROBLEMA INICIAL

### Contexto do Negócio

A empresa necessitava calcular distâncias rodoviárias reais entre pontos de venda (POCs) e endereços de entrega de pedidos para:
- Detecção de fraudes em entregas
- Otimização de rotas logísticas
- Análise de viabilidade de zonas de entrega
- Cálculo preciso de custos de frete

### Desafios Técnicos Identificados

**1. Volume de Dados**
- ~6 milhões de registros/dia
- Dados históricos desde 2024 (~72M+ registros)
- Processamento diário contínuo

**2. Limitações de Infraestrutura**
- Servidor OSRM em VM EC2 com disco limitado (30GB)
- Necessidade de processamento paralelo massivo
- Risco de esgotamento de espaço em disco

**3. Complexidade de Processamento**
- Duplicatas em múltiplas camadas (fonte, processamento, histórico)
- Necessidade de processamento incremental vs histórico
- Sincronização entre pipelines upstream (Spark) e downstream (Airflow)

**4. Falta de Observabilidade**
- Sem notificações automáticas de falha
- Logs dispersos e difíceis de analisar
- Dificuldade em diagnosticar problemas

---

## 🎯 OBJETIVOS DA SOLUÇÃO

### Requisitos Funcionais

**1. Processamento Automatizado**
- Execução diária sem intervenção manual
- Tolerância a falhas com retry automático
- Recuperação de checkpoints em caso de interrupção

**2. Otimização de Recursos**
- Máximo aproveitamento de CPU/memória disponível
- Processamento paralelo eficiente
- Gerenciamento inteligente de espaço em disco

**3. Deduplicação Inteligente**
- Remoção de duplicatas na fonte
- Dedupe incremental do mês corrente
- Consolidação de meses históricos

**4. Integração com Ecossistema**
- Sincronização com pipeline Spark (50-ze-datalake-refined)
- Compatibilidade com Batch Processor (Airflow DAG)
- Estrutura de dados compatível com Delta Lake

### Requisitos Não-Funcionais

**1. Performance**
- Processar 6M registros em < 20 minutos
- Throughput > 5.000 registros/segundo
- Latência média < 100ms por request OSRM

**2. Confiabilidade**
- Disponibilidade > 99% (considerando janela de execução)
- Taxa de sucesso > 95%
- Recuperação automática de falhas transitórias

**3. Escalabilidade**
- Suporte a crescimento de 50% ao ano
- Adaptação automática a picos de volume
- Reprocessamento histórico sem impacto no incremental

**4. Observabilidade**
- Notificações em tempo real de sucesso/falha
- Logs estruturados e auditáveis
- Métricas de performance detalhadas

---

## 🏗️ ARQUITETURA FINAL (HIGH-LEVEL)

### Componentes Principais

```
┌─────────────────────────────────────────────────────────────────────┐
│                          PIPELINE OSRM DISTANCE                     │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────────┐
│   UPSTREAM       │
│   (Pipeline 50)  │  ← Gera dados diariamente às 03:00
│   Spark Job      │
└────────┬─────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SOURCE: s3://50-ze-datalake-refined/                               │
│          data_mesh/vw_antifraud_fact_distances/YYYY-MM/             │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  ORQUESTRAÇÃO AWS                                                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐           │
│  │ EventBridge  │ ──▶│   Lambda     │ ──▶│   EC2 VM     │          │
│  │ (Cron 04:00) │    │ (Auto-Start) │    │ (OSRM Server)│           │
│  └──────────────┘    └──────────────┘    └──────┬───────┘           │
│                                                    │                │
│                                          ┌─────────▼──────────┐     │
│                                          │  @reboot Trigger   │     │
│                                          │  osrm_run.sh       │     │
│                                          └─────────┬──────────┘     │
└────────────────────────────────────────────────────┼─────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PROCESSAMENTO PRINCIPAL                                            │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ osrm-request.py (Pipeline Principal)                          │  │
│  │ • Detecta modo: HISTÓRICO vs INCREMENTAL                      │  │
│  │ • Processamento paralelo (15 processos × 30 requests)         │  │
│  │ • Chunks de 1.5M registros                                    │  │
│  │ • Dedupe na fonte + validação                                 │  │
│  │ • Upload com nomes únicos (hash-based)                        │  │
│  └──────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│                             ▼                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ dedupe_current_month.py (Dedupe Diário)                       │  │
│  │ • Consolida arquivos do dia                                   │  │
│  │ • Remove duplicatas globais                                   │  │
│  │ • Substitui múltiplos arquivos por 1 dedupe único             │  │
│  └──────────────────────────────────────────────────────────────┘   │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LANDING ZONE: s3://20-ze-datalake-landing/                         │
│                osrm_distance/osrm_landing/                          │
│                                                                     │
│  Estrutura:                                                         │
│  year=2025/                                                         │
│  ├── month=01/                                                      │
│  │   └── consolidated-2025-01.parquet (HISTÓRICO)                   │
│  ├── month=11/                                                      │
│  │   └── consolidated-2025-11.parquet (HISTÓRICO)                   │
│  └── month=12/ (MÊS CORRENTE)                                       │
│      ├── dedupe_abc123_000.parquet (Dia 01)                         │
│      ├── dedupe_def456_000.parquet (Dia 02)                         │
│      └── dedupe_ghi789_000.parquet (Dia 03)                         │
└────────┬────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  MONITORAMENTO & NOTIFICAÇÕES                                       │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐           │
│  │  osrm_run.sh │ ──▶│  S3 Events   │ ──▶│   Lambda     │          │
│  │  (Salva Logs)│    │ (Trigger)    │    │  (Monitor)   │           │
│  └──────────────┘    └──────────────┘    └──────┬───────┘           │
│                                                    │                │
│                                          ┌─────────▼──────────┐     │
│                                          │   SNS Topic        │     │
│                                          │   (E-mail Alert)   │     │
│                                          └────────────────────┘     │
└─────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DOWNSTREAM: Batch Processor (Airflow)                               │
│  DAG: batch-processor-engine-group-osrm_distance                     │
│  • Detecta novos arquivos às 04:30                                   │
│  • Processa para camada 30 (Delta Lake)                             │
│  • Disponibiliza para consumo analítico                              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🔄 FLUXO DE DADOS (END-TO-END)

### Dia Típico (Modo Incremental)

**03:00** - Pipeline da 50 (Spark)
```
Entrada: Tabela fact_orders (Snowflake)
Saída: s3://50-ze-datalake-refined/.../2025-12/part-00000.parquet
Volume: ~6M registros
```

**04:00** - EventBridge dispara Lambda
```
Lambda verifica status da VM → START
VM inicia → Container OSRM sobe
Health check: espera porta 5000
```

**04:02** - Pipeline OSRM (osrm-request.py)
```
1. Lê bookmark.json
2. Detecta modo: INCREMENTAL (mês corrente)
3. Filtra apenas arquivos novos (LastModified > checkpoint)
4. Processa em blocos de 1.5M
5. Upload: part-{hash}-{idx}-{chunk}.parquet
6. Atualiza delta_timestamp no bookmark
```

**04:17** - Dedupe do Dia (dedupe_current_month.py)
```
1. Lê TODOS os arquivos de 2025-12/
2. Concatena e remove duplicatas por order_number
3. Salva: dedupe_{hash}_{idx}.parquet
4. Deleta arquivos part-* originais
```

**04:19** - Finalização
```
osrm_run.sh:
  - Salva log em s3://osrm_success/
  - Lambda detecta → SNS envia e-mail
  - VM desliga automaticamente
```

**04:30** - Batch Processor (Airflow)
```
Watcher detecta novo arquivo dedupe_*.parquet
Dispara DAG batch-processor-engine-group-osrm_distance
Processa para Delta Lake (camada 30)
```

---

## 📈 DIFERENCIAL DA SOLUÇÃO

### Inovações Técnicas

**1. Processamento Híbrido (Histórico + Incremental)**
- Detecção automática do tipo de job
- Checkpoint granular por partição
- Zero reprocessamento desnecessário

**2. Deduplicação em 3 Camadas**
- **Camada 1:** Na leitura do arquivo fonte (drop_duplicates)
- **Camada 2:** Durante processamento (hash único por arquivo)
- **Camada 3:** Consolidação diária (dedupe_current_month.py)

**3. Orquestração Inteligente**
- Auto-start/stop de VM (economia de 22h/dia)
- Sincronização precisa entre componentes (margem de 11-13min)
- Recuperação automática de falhas transitórias

**4. Observabilidade Nativa**
- Logs estruturados em S3 (imutáveis, auditáveis)
- Notificações contextuais (sucesso detalhado, falha diagnosticada)
- Métricas de performance em tempo real

---

## 🎯 PRÓXIMAS SEÇÕES

- **Seção 2:** Evolução da Solução (cronologia das implementações)
- **Seção 3:** Arquitetura Técnica (deep dive em cada componente)
- **Seção 4:** Lógica de Processamento (algoritmos e estratégias)
- **Seção 5:** Infraestrutura AWS (Terraform e orquestração)
- **Seção 6:** Monitoramento & Notificações (sistema de alertas)
- **Seção 7:** Resultados & Métricas (performance e custos)

---

**Última Atualização:** 01/12/2025  
**Versão:** 1.0  
**Autor:** Time de Engenharia de Dados
