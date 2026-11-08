# ControllerASG

Un **controlador de auto-scaling horizontal autónomo** escrito en Python: un sistema autogestionado de elasticidad para aplicaciones en la nube sobre Amazon Web Services (AWS).

El controlador gestiona de forma autónoma instancias EC2 individuales (identificadas mediante el tag `managed-by=controllerasg`): consulta métricas en Amazon CloudWatch, evalúa si es necesario modificar la capacidad para mantener el nivel de servicio y actúa directamente sobre EC2 y el Target Group de un Application Load Balancer (ALB). Cada decisión tomada queda auditada y explicada en un archivo de log estructurado en formato JSON Lines.

---

## Índice

- [Resumen](#resumen)
- [Funcionalidades](#funcionalidades)
- [Arquitectura](#arquitectura)
- [Flujo de ejecución](#flujo-de-ejecución)
- [Lógica de decisión](#lógica-de-decisión)
- [Tecnologías](#tecnologías)
- [Requisitos previos](#requisitos-previos)
- [Instalación](#instalación)
  - [1. Instalar dependencias](#1-instalar-dependencias)
  - [2. Credenciales y permisos de AWS](#2-credenciales-y-permisos-de-aws)
  - [3. Provisionar la infraestructura de prueba (opcional)](#3-provisionar-la-infraestructura-de-prueba-opcional)
  - [4. Configurar y arrancar con init.sh](#4-configurar-y-arrancar-con-initsh)
- [Configuración (conf.json)](#configuración-confjson)
- [Uso](#uso)
- [El log de decisiones](#el-log-de-decisiones)
- [Ejecutar en una instancia EC2](#ejecutar-en-una-instancia-ec2)
- [Tests](#tests)
- [Capacidades y Mecanismos de Control](#capacidades-y-mecanismos-de-control)
- [Limitaciones y notas](#limitaciones-y-notas)
- [Estructura del proyecto](#estructura-del-proyecto)

---

## Resumen

ControllerASG implementa un **bucle de control cerrado** (*feedback control loop*: observar → decidir → actuar) ejecutado periódicamente:

1. **Observa** el estado real de la flota (instancias EC2 gestionadas y su salud en el Target Group) y sus métricas operativas en CloudWatch.
2. **Decide** si aumentar, reducir o mantener la capacidad, según umbrales configurables y reglas de estabilidad (mínimos de seguridad, enfriamiento y calentamiento de instancias).
3. **Actúa** aprovisionando o terminando instancias, registrándolas o desregistrándolas del Target Group, asegurando drenaje de conexiones activas y *rollback* ante fallos.

Toda observación, decisión y acción ejecutada se anota como un registro JSON en el log de auditoría para permitir trazabilidad completa.

---

## Funcionalidades

- **Escalado horizontal automático** de instancias EC2 entre un mínimo y un máximo configurables (1 a 5 por defecto).
- **Múltiples métricas simultáneas** de CloudWatch, combinables:
  - `RequestCountPerTarget` — demanda directa (requests entrantes por instancia).
  - `TargetResponseTime` — calidad de servicio y SLO (latencia de respuesta).
  - `CPUUtilization` — uso de cómputo (por instancia, se promedia entre la flota).
- **Escalado asimétrico y conservador**: sube si *cualquier* métrica supera su umbral (reacción rápida ante sobrecarga); baja solo si *todas* están por debajo de su umbral (protección de disponibilidad).
- **Reglas de seguridad y estabilidad**:
  - Piso de capacidad garantizado (restablece el mínimo configurado incluso sin métricas).
  - *Cooldown* entre acciones para evitar oscilaciones rápidas (*flapping*).
  - No reduce capacidad mientras haya instancias inicializándose.
- **Operaciones seguras sobre la infraestructura**:
  - Registro en el Target Group y espera a estado `running` al escalar hacia arriba.
  - *Rollback* automático (termina la instancia) si no llega a estar operativa.
  - Drenaje de conexiones activas antes de terminar instancias al reducir.
  - Distribución equilibrada de instancias entre zonas de disponibilidad (subredes).
- **Auditoría completa**: un `DecisionRecord` en formato JSON Lines por cada ciclo.
- **Validación de configuración** independiente (`-check`) sin contactar AWS.
- **Servidor HTTP integrado**: endpoints `/health` y `/status` para supervisión y monitoreo externo.
- **Apagado limpio** ante `SIGINT`/`SIGTERM` (compatible con systemd y Ctrl+C).
- **Script interactivo** (`init.sh`) para asistir en la configuración, validación y arranque.

---

## Arquitectura

El diseño desacopla responsabilidades en módulos independientes, comunicados mediante clases bien definidas:

```
                         ┌───────────────────────────────────────────┐
                         │              main.py / CLI                │
                         │   argumentos, dependencias, ticker, señales │
                         └───────────────────────┬───────────────────┘
                                                 │
                               ┌──────────────────▼──────────────────┐
                               │             controller              │
                               │  tick(): observar → decidir → actuar│
                               │  y registra 1 DecisionRecord        │
                               └──┬───────────┬───────────┬──────────┘
                                  │           │           │
              MetricsSource ──────┘           │           └───── Actuator / Fleet
                    │                         │                        │
            ┌───────▼────────┐        ┌───────▼──────┐         ┌───────▼────────┐
            │    observer    │        │    logger    │         │   provisioner  │
            │  CloudWatch    │        │  JSON Lines  │         │  EC2 + ELBv2   │
            │  GetMetricData │        └──────────────┘         │  run/terminate │
            └───────┬────────┘                                 │  register/     │
                    │                                          │  deregister    │
              ┌─────▼─────┐                                    └───────┬────────┘
              │ CloudWatch│                                       ┌────▼────┐
              └───────────┘                                       │ EC2/ALB │
                                                                  └─────────┘
```

Módulos principales:

| Módulo | Responsabilidad |
|--------|-----------------|
| `controllerasg.main` | Punto de entrada: parsea flags, inicializa clientes de AWS, ejecuta el ticker y gestiona señales del sistema. |
| `controllerasg.configuration` | Carga, asigna valores por defecto y **valida** estrictamente `conf.json`. |
| `controllerasg.controller` | Orquesta el ciclo de control y contiene la **lógica de decisión**, cooldown e histéresis. |
| `controllerasg.observer` | Traduce las políticas de métrica a consultas `GetMetricData` de CloudWatch, con paginación y agregación por ventana. |
| `controllerasg.provisioner` | Gestiona el ciclo de vida de instancias en EC2 y el registro/drenaje en el Target Group (ELBv2). |
| `controllerasg.logger` | Escribe los registros `DecisionRecord` por línea en JSON Lines de forma segura para hilos. |
| `controllerasg.statusserver` | Expone un micro-servidor HTTP con los endpoints `/health` y `/status`. |
| `controllerasg.model` | Modelos de datos: `Instance`, `ObservedMetric`, `Decision`, `DecisionRecord`, `Capacity`. |
| `infra` | Scripts para aprovisionar y destruir infraestructura de prueba (VPC, SG, launch template, target group, ALB). |

---

## Flujo de ejecución

En cada tick (determinado por `pollIntervalSeconds`), `controller.tick()`:

1. **Lee la flota** con `provisioner.instances()`: instancias en estado `pending`/`running` con el tag `managed-by=controllerasg` y su salud en el Target Group. Si la consulta falla, se aborta la decisión para evitar acciones a ciegas.
2. **Observa las métricas** con `observer.observe()`. Las métricas sin datapoints se omiten (no se asumen en 0).
3. **Decide** la acción (`INCREASE_CAPACITY`, `REDUCE_CAPACITY` o `MAINTAIN_CAPACITY`).
4. **Actúa** si corresponde:
   - *ScaleUp*: lanza instancias con `run_instances` usando el Launch Template → espera a estado `running` → registra en el Target Group con `register_targets`. Si algo falla, ejecuta *rollback* (termina la instancia).
   - *ScaleDown*: selecciona la instancia víctima → desregistra del Target Group con `deregister_targets` → espera a que drene conexiones activas → termina la instancia con `terminate_instances`.
5. **Registra** exactamente un `DecisionRecord` en el log, documentando observaciones, motivo, acción y resultado.

---

## Lógica de decisión

El orden de evaluación en `controller.decide()` garantiza estabilidad y disponibilidad:

1. **Piso de seguridad**: Si `capacidad < minInstances`, sube capacidad inmediatamente, ignorando métricas y cooldown (cubre arranque en frío o pérdida imprevista de instancias).
2. **Cooldown**: Si la última acción ocurrió hace menos de `cooldownSeconds`, mantiene la capacidad.
3. **Sin métricas**: Si no se recibieron datos de métricas, mantiene la capacidad.
4. **Umbrales**:
   - **Sube** (`INCREASE_CAPACITY`) si *alguna* métrica ≥ su `scaleUpThreshold` (y la flota es menor a `maxInstances`).
   - **Reduce** (`REDUCE_CAPACITY`) si *todas* las métricas < su `scaleDownThreshold` durante `confirmCyclesDown` ciclos consecutivos (y la flota es mayor a `minInstances`).
   - En cualquier otro caso, mantiene (`MAINTAIN_CAPACITY`).

Una métrica sin datos **se abstiene**: no fuerza escalado hacia arriba ni bloquea la reducción. En métricas de demanda, la ausencia de tráfico indica que es seguro reducir o mantener la capacidad.

---

## Tecnologías

- **Python 3.10+** (probado y compatible con Python 3.10 a 3.14).
- **AWS SDK for Python (`boto3`)**:
  - `ec2` — lanzar, terminar y describir instancias.
  - `elbv2` — registrar/desregistrar targets y consultar su salud.
  - `cloudwatch` — consultas agregadas de series de tiempo con `GetMetricData`.
- **pytest** — suite de pruebas unitarias y de integración.
- **matplotlib** — generación de gráficas y visualización temporal en `analysis/analyze.py`.
- **Bash** — scripts auxiliares de configuración e infraestructura en `infra/`.
- **AWS CLI** — utilizado en los scripts de `infra/` para aprovisionamiento inicial.

---

## Requisitos previos

- **Python 3.10 o superior** y gestor de paquetes `pip`.
- **Credenciales de AWS** configuradas en el entorno (`~/.aws/credentials` o variables de entorno `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`).
- **AWS CLI** — necesario solo si se usarán los scripts de automatización de `infra/`.
- Recursos de AWS disponibles (o creados con `infra/up.sh`):
  - un **Launch Template**,
  - un **Target Group** asociado a un **Application Load Balancer**,
  - al menos una **subred** (recomendado dos en zonas de disponibilidad distintas).

---

## Instalación

### 1. Instalar dependencias

Instalar los paquetes requeridos de Python:

```bash
pip install -r requirements.txt
```

O en un entorno virtual aislado:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Credenciales y permisos de AWS

En local, configurar las credenciales mediante `aws configure`. En una instancia EC2, asignar un **IAM Instance Role** con la siguiente política mínima:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:RunInstances",
        "ec2:TerminateInstances",
        "ec2:DescribeInstances",
        "ec2:CreateTags",
        "elasticloadbalancing:RegisterTargets",
        "elasticloadbalancing:DeregisterTargets",
        "elasticloadbalancing:DescribeTargetHealth",
        "cloudwatch:GetMetricData"
      ],
      "Resource": "*"
    }
  ]
}
```

### 3. Provisionar la infraestructura de prueba (opcional)

Si no cuentas con un Launch Template y un ALB con Target Group ya configurados, puedes levantarlos con el script automatizado:

```bash
cd infra
./up.sh          # crea SG, launch template, target group y ALB
```

Para transferir los identificadores generados a un archivo `conf.json`:

```bash
./write-conf.sh
```

Para desmantelar los recursos al concluir las pruebas:

```bash
./down.sh
```

### 4. Configurar y arrancar con init.sh

El asistente `init.sh` guía paso a paso la configuración:

```bash
./init.sh
```

---

## Configuración (conf.json)

El archivo `conf.json` especifica los parámetros del controlador. Ejemplo:

```json
{
  "aws": { "region": "us-east-1" },
  "compute": {
    "launchTemplateId": "lt-0123456789abcdef0",
    "launchTemplateVersion": "$Latest",
    "minInstances": 1,
    "maxInstances": 5,
    "subnetIds": ["subnet-aaaa", "subnet-bbbb"],
    "securityGroupIds": []
  },
  "loadBalancer": {
    "targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mi-tg/xxxx",
    "targetPort": 80
  },
  "pollIntervalSeconds": 60,
  "observationWindowSeconds": 300,
  "cooldownSeconds": 300,
  "scaleUpStep": 1,
  "scaleDownStep": 1,
  "confirmCyclesDown": 3,
  "statusPort": 8080,
  "stateFile": "controller-state.json",
  "metrics": [
    {
      "namespace": "AWS/ApplicationELB",
      "metricName": "RequestCountPerTarget",
      "dimensions": [
        { "name": "TargetGroup",  "value": "targetgroup/mi-tg/xxxx" },
        { "name": "LoadBalancer", "value": "app/mi-alb/yyyy" }
      ],
      "period": 60,
      "stat": "Sum",
      "scaling": { "scaleUpThreshold": 1000, "scaleDownThreshold": 200 }
    },
    {
      "namespace": "AWS/EC2",
      "metricName": "CPUUtilization",
      "perInstance": true,
      "period": 60,
      "stat": "Average",
      "scaling": { "scaleUpThreshold": 70, "scaleDownThreshold": 20 }
    }
  ],
  "deployment": { "useDocker": false }
}
```

Parámetros:

| Parámetro | Descripción |
|-----------|-------------|
| `aws.region` | Región de AWS. **Obligatorio.** |
| `compute.launchTemplateId` | Launch template para crear instancias. **Obligatorio.** |
| `compute.launchTemplateVersion` | Versión del Launch Template (`$Latest`, `$Default` o número). |
| `compute.minInstances` / `maxInstances` | Límites de capacidad (`1 ≤ min ≤ max ≤ 5`). |
| `compute.subnetIds` | Subredes donde balancear instancias (al menos 1). |
| `compute.securityGroupIds` | Security groups explícitos (vacío usa los del template). |
| `loadBalancer.targetGroupArn` | Target Group donde registrar instancias. **Obligatorio.** |
| `loadBalancer.targetPort` | Puerto de la aplicación en las instancias. |
| `pollIntervalSeconds` | Segundos entre ciclos de evaluación (`> 0`). |
| `observationWindowSeconds` | Ventana de observación para CloudWatch (`≥ period` de cada métrica). |
| `cooldownSeconds` | Tiempo mínimo de espera entre acciones de escalado (`≥ 0`). |
| `scaleUpStep` / `scaleDownStep` | Cantidad de instancias a añadir o retirar por acción (1 o 2). |
| `confirmCyclesDown` | Ciclos consecutivos requeridos bajo umbral antes de reducir (`≥ 1`). |
| `statusPort` | Puerto del servidor HTTP para `/health` y `/status` (0 deshabilita). |
| `stateFile` | Archivo para persistir el timestamp de cooldown entre reinicios. |
| `metrics[]` | Políticas de métricas configuradas (al menos 1). |
| `metrics[].perInstance` | Si es `true`, se consulta por cada instancia y se promedia la flota. |
| `metrics[].period` | Múltiplo positivo de 60 segundos. |
| `metrics[].scaling` | Umbrales donde `scaleUpThreshold > scaleDownThreshold`. |

---

## Uso

### Validar la configuración sin contactar AWS:

```bash
./bin/controllerasg -config conf.json -check
# o directamente con python:
python3 main.py -config conf.json -check
```

### Iniciar el controlador:

```bash
./bin/controllerasg -config conf.json -log decisions.jsonl
# o directamente con python:
python3 main.py -config conf.json -log decisions.jsonl
```

### Consultar el estado HTTP:

Si `statusPort` está habilitado (p. ej. 8080):

```bash
curl http://localhost:8080/health
curl http://localhost:8080/status | jq .
```

---

## El log de decisiones

Cada ciclo evaluado produce una entrada estructurada en formato JSON Lines (`decisions.jsonl`):

```json
{
  "CycleID": 12,
  "Time": "2026-09-22T16:01:48.907470-05:00",
  "Decision": "REDUCE_CAPACITY",
  "Reason": "todas las métricas bajo umbral durante los ciclos de confirmación requeridos",
  "WindowSeconds": 300,
  "Metrics": [
    { "Name": "RequestCountPerTarget", "Value": 0, "timestamp": "2026-09-22T16:00:00-05:00" },
    { "Name": "CPUUtilization", "Value": 0.12, "timestamp": "2026-09-22T16:00:00-05:00" }
  ],
  "CapacityBefore": {
    "Total": 2, "Healthy": 2,
    "Instances": [
      { "ID": "i-0aad...", "State": "running", "Health": "healthy" },
      { "ID": "i-0866...", "State": "running", "Health": "healthy" }
    ]
  },
  "Capacity": {
    "Total": 2, "Healthy": 2,
    "Instances": [
      { "ID": "i-0aad...", "State": "running", "Health": "healthy" },
      { "ID": "i-0866...", "State": "running", "Health": "healthy" }
    ]
  },
  "CapacityAfter": {
    "Total": 1, "Healthy": 1,
    "Instances": [
      { "ID": "i-0aad...", "State": "running", "Health": "healthy" }
    ]
  },
  "ConfirmCycle": 3,
  "InstanceID": "i-0866...",
  "Action": "ScaleDown",
  "Result": "ok",
  "Error": ""
}
```

Para monitorear el log en tiempo real:

```bash
tail -f decisions.jsonl | jq '{t: .Time, d: .Decision, r: .Reason, cap: .CapacityBefore.Total}'
```

Para generar estadísticas y gráficas visuales:

```bash
python3 analysis/analyze.py decisions.jsonl
```

---

## Ejecutar en una instancia EC2

Para ejecutar el controlador en una máquina virtual dedicada con **systemd**:

1. Crear el archivo de unidad `/etc/systemd/system/controllerasg.service`:

```ini
[Unit]
Description=ControllerASG Auto-Scaling Controller
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ControllerASG
ExecStart=/usr/bin/python3 /opt/ControllerASG/main.py -config /opt/ControllerASG/conf.json -log /opt/ControllerASG/decisions.jsonl
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

2. Habilitar e iniciar el servicio:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now controllerasg
journalctl -u controllerasg -f
```

---

## Tests

La suite de pruebas automatizadas está construida con **pytest**:

```bash
# Ejecutar todas las pruebas unitarias:
pytest

# Con reporte detallado:
pytest -v

# Pruebas con cobertura:
pytest --cov=controllerasg tests/
```

Para ejecutar las pruebas de integración contra recursos activos de AWS (opcional):

```bash
# Integración con CloudWatch:
TEST_INSTANCE_ID=i-xxxx pytest -m integration tests/test_observer_integration.py

# Integración con EC2 y Target Group:
CONFIG_PATH=conf.json pytest -m integration -s tests/test_provisioner_integration.py
```

---

## Capacidades y Mecanismos de Control

1. **Histéresis temporal para ScaleDown (`confirmCyclesDown`)**: Requiere $N$ ciclos consecutivos con todas las métricas bajo el umbral antes de retirar una instancia, eliminando oscilaciones por variaciones transitorias de carga (*flapping*).
2. **Pasos de escala configurables (`scaleUpStep` / `scaleDownStep`)**: Permite ajustar cuántas instancias se lanzan o retiran por acción según la agresividad requerida.
3. **Persistencia del Cooldown (`stateFile`)**: Guarda en disco `lastActionAt` en formato JSON, permitiendo que el estado de enfriamiento sobreviva a reinicios del proceso o fallos de servicio.
4. **Endpoint de salud y estado HTTP (`statusPort`)**: Expone un micro-servidor HTTP (`GET /health` y `GET /status`) para chequeos de salud de systemd, load balancers o paneles de observabilidad sin necesidad de parsear archivos de log.
5. **Auditoría enriquecida**: Los registros en `decisions.jsonl` incluyen `CycleID` secuencial, `ConfirmCycle` acumulado y `CapacityBefore`/`CapacityAfter` para trazabilidad inmediata.
6. **Generador de carga para pruebas (`infra/load.sh`)**: Script automatizado multi-fase compatible con Apache Bench (`ab`) y `hey`.
7. **Política IAM mínima (`infra/iam-policy.json`)**: Política de mínimo privilegio con las acciones exactas requeridas por el controlador.
8. **Herramienta de análisis y visualización (`analysis/analyze.py`)**: Analizador estadístico y generador de gráficas temporales de capacidad y métricas.
9. **Documentación técnica y taxonómica**:
   - `docs/taxonomy.md`: Clasificación taxonómica formal según Al-Dhuraibi et al.
   - `docs/critical-analysis.md`: Análisis crítico de desempeño, SLO, costos y sustentación.

---

## Limitaciones y notas

- **Coordinación centralizada**: No incluye mecanismo de consenso distribuido distribuido (p. ej. DynamoDB o etcd) ni elección de líder: se debe ejecutar una única instancia del controlador por Target Group.
- **Sincronismo de aprovisionamiento**: Al realizar un *ScaleUp*, el proceso espera hasta que la instancia alcanza estado `running` antes de reanudar el bucle principal.
- **Rango de flota**: Diseñado para flotas de cómputo elásticas de entre 1 y 5 instancias, acorde a cuotas académicas y de laboratorio.

---

## Estructura del proyecto

```
ControllerASG/
├── controllerasg/       # Paquete principal en Python
│   ├── configuration.py # Carga, valores por defecto y validación de conf.json
│   ├── controller.py    # Bucle observar→decidir→actuar, histéresis y persistencia
│   ├── logger.py        # Log estructurado de decisiones en JSON Lines
│   ├── main.py          # Lógica de arranque, bucle ticker y captura de señales
│   ├── model.py         # Modelos de dominio (DecisionRecord, Instance, Metric)
│   ├── observer.py      # Consulta y agregación de métricas en CloudWatch
│   ├── provisioner.py   # Gestión de ciclo de vida en EC2 y ELBv2
│   └── statusserver.py  # Servidor HTTP (/health, /status)
├── tests/               # Suite completa de pruebas unitarias y de integración
│   ├── test_configuration.py
│   ├── test_controller.py
│   ├── test_observer.py
│   ├── test_provisioner.py
│   ├── test_statusserver.py
│   ├── test_observer_integration.py
│   └── test_provisioner_integration.py
├── main.py              # Script ejecutable de entrada
├── bin/
│   └── controllerasg    # Ejecutable directo CLI (Python)
├── docs/                # Entregables teóricos y de análisis
│   ├── taxonomy.md          # Taxonomía según Al-Dhuraibi et al.
│   └── critical-analysis.md # Análisis crítico, SLO, costos y evaluación
├── infra/               # Infraestructura como código y pruebas de carga
│   ├── up.sh                # Provisiona VPC, SG, ALB, TG, Launch Template
│   ├── down.sh              # Destruye la infraestructura de prueba
│   ├── load.sh              # Generador de carga multi-fase
│   ├── iam-policy.json      # Política IAM de mínimo privilegio
│   └── user-data.sh         # Script de arranque de Nginx en instancias
├── analysis/            # Herramientas de evaluación
│   └── analyze.py           # Análisis estadístico y gráfico de decisions.jsonl
├── init.sh              # Asistente interactivo: configurar + validar + arrancar
├── requirements.txt     # Dependencias de Python (boto3, pytest, matplotlib)
├── conf.example.json    # Plantilla de configuración documentada
└── decisions.jsonl      # Log auditable de decisiones tomadas (generado en ejecución)
```
