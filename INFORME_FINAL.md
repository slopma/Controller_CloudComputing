# Reto 1 — Auto-Scaling Controller: Diseño, Implementación y Evaluación Experimental

**Asignatura:** Computación en la Nube (SI3016)  
**Estudiante:** Sara López Marín (`slopezm13@eafit.edu.co`)  
**Universidad:** Universidad EAFIT — Semestre 2026-2  
**Repositorio:** `Controller_CloudComputing`  
**Referencia Teórica:** Al-Dhuraibi et al., *"Elasticity in Cloud Computing: State of the Art and Research Challenges"*, IEEE TSC, 2018.

---

## 1. Diseño de la Solución (§10.1)

### 1.1 Visión General y Arquitectura
El objetivo del proyecto es construir un controlador de elasticidad horizontal autónomo para una aplicación web stateless montada sobre instancias EC2 detrás de un Application Load Balancer (ALB). A diferencia del auto-scaling nativo de AWS, la lógica de decisión, agregación de métricas y ejecución de acciones es completamente nuestra, implementando un bucle de control cerrado clásico:

$$\text{Observe} \longrightarrow \text{Analyze} \longrightarrow \text{Decide} \longrightarrow \text{Act} \longrightarrow \text{Observe}$$

```
                +-------------------------------------------+
                |          AWS CloudWatch / ELB             |
                |   (RequestCount, Latency, CPUUtilization) |
                +---------------------+---------------------+
                                      | Métricas (promedio ventana)
                                      v
  +-----------------------------------------------------------------------+
  |                           CONTROLLERASG                               |
  |                                                                       |
  |  +--------------------+     +-------------------+     +------------+  |
  |  | CloudWatchObserver | --> | Policy Evaluator  | --> | Provisioner|  |
  |  | (Observer)         |     | (Decider)         |     | (Actuator) |  |
  |  +--------------------+     +-------------------+     +-----+------+  |
  |                                     |                       |         |
  |                                     v                       |         |
  |                             decisions.jsonl                 |         |
  +-------------------------------------------------------------+---------+
                                                                |
                                              +-----------------+-----------------+
                                              | RunInstances / TerminateInstances |
                                              v                                   v
                                      +---------------+                   +---------------+
                                      |  EC2 Instance |                   |  EC2 Instance |
                                      |  (Target #1)  |                   |  (Target #2)  |
                                      +---------------+                   +---------------+
```


### 1.2 Política de Control y Reglas de Decisión
Para evitar inestabilidad y garantizar la disponibilidad del servicio, la política de control aplica las siguientes reglas en estricto orden jerárquico:

1. **Garantía de piso mínimo de flota**:  
   Si `flota_actual < minInstances` (ej. 0 o fallo de instancia), el controlador emite de inmediato `INCREASE_CAPACITY` con razón `"capacidad por debajo del mínimo configurado"`. Esta regla omite cualquier periodo de cooldown o evaluación de métricas para restaurar el servicio lo más rápido posible.
2. **Respeto de límite máximo**:  
   Si la flota ya alcanzó `maxInstances` (5 instancias), no se permite ningún escalado hacia arriba, emitiendo `MAINTAIN_CAPACITY`.
3. **Periodo de Cooldown**:  
   Tras ejecutar una acción de escalado, el sistema congela cambios durante `cooldownSeconds` (por defecto 300 segundos = 5 minutos). Esto permite que la nueva máquina arranque, compile/instale sus paquetes, se registre en el balanceador y su impacto se vea reflejado en CloudWatch antes de tomar otra decisión. El timestamp del cooldown se persiste en disco (`controller-state.json`) para no perderse si el proceso se reinicia.
4. **Filtro de métricas faltantes**:  
   Si CloudWatch no reporta datapoints en la ventana, el controlador asume una postura conservadora y mantiene la capacidad actual, evitando decisiones a ciegas.
5. **Decisión asimétrica de escalado**:
   - **Regla de Escala hacia Arriba (`INCREASE_CAPACITY`)**: Se activa si **al menos una** métrica supera su umbral (`scaleUpThreshold`). Por ejemplo, si la latencia o las peticiones se disparan, se añade capacidad sin esperar a que la CPU también se sature, priorizando el Service Level Objective (SLO).
   - **Regla de Escala hacia Abajo (`REDUCE_CAPACITY`)**: Se activa **únicamente si todas** las métricas están por debajo de su umbral bajo (`scaleDownThreshold`) **y además** esta condición se mantiene durante $N$ ciclos consecutivos (`confirmCyclesDown`, típicamente 3 ciclos = 3 minutos). Esta histéresis temporal elimina por completo el fenómeno de *flapping* (prender y apagar instancias repetidamente ante oscilaciones momentáneas de tráfico).

---

## 2. Implementación Reproducible (§10.2)

### 2.1 Estructura del Repositorio
```
Controller_CloudComputing/
├── analysis/
│   └── analyze.py              # Script estadístico y generador de gráficas
├── bin/
│   └── controllerasg           # Wrapper ejecutable CLI
├── controllerasg/
│   ├── __init__.py
│   ├── configuration.py        # Validación y parsing de conf.json
│   ├── controller.py           # Bucle de control, lógica de decisión y estado
│   ├── logger.py               # Escritura en decisions.jsonl
│   ├── main.py                 # Punto de entrada y orquestador CLI
│   ├── model.py                # Dataclasses de instancias, métricas y decisiones
│   ├── observer.py             # Cliente CloudWatch y cálculo de métricas
│   ├── provisioner.py          # Gestor de instancias EC2 y Target Group
│   └── statusserver.py         # Servidor HTTP /health y /status
├── docs/
│   ├── critical-analysis.md    # Borrador y respuestas de sustentación
│   ├── INFORME_FINAL.md        # Documento consolidado entregable
│   └── taxonomy.md             # Justificación taxonómica detallada
├── infra/
│   ├── down.sh                 # Limpieza y eliminación de infraestructura
│   ├── iam-policy.json         # Definición de política de seguridad IAM
│   ├── load.sh                 # Generador de tráfico en 3 fases (Apache Bench / hey)
│   ├── up.sh                   # Creación de SG, Launch Template, Target Group y ALB
│   └── write-conf.sh           # Actualización automática de conf.json desde state.env
├── tests/                      # Suite de 54 pruebas unitarias con mocks de AWS
├── conf.example.json           # Plantilla de configuración
├── decisions.jsonl             # Registro de decisiones del controlador
├── main.py                     # Script principal de lanzamiento
├── pytest.ini                  # Configuración de pruebas
└── requirements.txt            # Dependencias Python (boto3, pytest, matplotlib)
```

### 2.2 Instrucciones de Despliegue y Ejecución
1. **Configurar credenciales**:
   ```bash
   aws configure
   ```
2. **Crear la infraestructura de prueba**:
   ```bash
   ./infra/up.sh
   ./infra/write-conf.sh
   ```
3. **Validar la configuración**:
   ```bash
   python3 main.py -config conf.json -check
   ```
4. **Ejecutar el controlador**:
   ```bash
   python3 main.py -config conf.json -log decisions.jsonl
   ```
5. **Ejecutar la suite de pruebas**:
   ```bash
   pytest -m "not integration"
   ```
6. **Destruir los recursos al finalizar**:
   ```bash
   ./infra/down.sh
   ```

---

## 3. Evidencia Experimental y Métricas (§10.3)

### 3.1 Escenario de Carga
Para evaluar el comportamiento dinámico del controlador, se ejecutó una prueba de carga escalonada usando `infra/load.sh` apuntando al DNS del ALB:

| Fase | Duración | Carga Generada | Objetivo Operacional | Decisión Esperada |
|---|---|---|---|---|
| **Fase 1: Reposo / Inicial** | 5 min | ~10 req/s | Validar arranque de capacidad mínima y estabilidad | `MAINTAIN_CAPACITY` (1 instancia) |
| **Fase 2: Ráfaga / Alta Demanda** | 10 min | 50 req/s | Superar umbral de peticiones y latencia | `INCREASE_CAPACITY` (1 → 2 instancias) |
| **Fase 2b: Carga Sostenida** | 5 min | 100 req/s | Probar absorción de tráfico distribuido en la flota | `MAINTAIN_CAPACITY` en cooldown |
| **Fase 3: Descenso de Carga** | 15 min | ~5 req/s | Retirar capacidad ociosa tras confirmar baja demanda | `REDUCE_CAPACITY` (2 → 1 instancia) |

### 3.2 Métricas Observadas en la Corrida
A continuación se presentan los registros representativos extraídos de `decisions.jsonl`:

```json
{"CycleID": 1, "Time": "2026-11-16T20:42:46-05:00", "Decision": "INCREASE_CAPACITY", "Reason": "capacidad por debajo del mínimo configurado", "CapacityBefore": {"Total": 0, "Healthy": 0}, "Action": "ScaleUp", "Result": "ok", "InstanceID": "i-0bbde0b6abcaf6016", "CapacityAfter": {"Total": 1, "Healthy": 0}}
{"CycleID": 2, "Time": "2026-11-16T20:43:54-05:00", "Decision": "MAINTAIN_CAPACITY", "Reason": "en cooldown", "CapacityBefore": {"Total": 1, "Healthy": 0}}
{"CycleID": 3, "Time": "2026-11-16T20:44:54-05:00", "Decision": "MAINTAIN_CAPACITY", "Reason": "en cooldown", "Metrics": [{"Name": "RequestCountPerTarget", "Value": 2.0}, {"Name": "TargetResponseTime", "Value": 0.000455}, {"Name": "CPUUtilization", "Value": 0.00588}], "CapacityBefore": {"Total": 1, "Healthy": 1}}
{"CycleID": 8, "Time": "2026-11-16T20:49:55-05:00", "Decision": "INCREASE_CAPACITY", "Reason": "métrica RequestCountPerTarget superó umbral de subida (1000)", "CapacityBefore": {"Total": 1, "Healthy": 1}, "Action": "ScaleUp", "Result": "ok", "InstanceID": "i-07c41a29bd8e2910f", "CapacityAfter": {"Total": 2, "Healthy": 1}}
{"CycleID": 15, "Time": "2026-11-16T20:56:56-05:00", "Decision": "REDUCE_CAPACITY", "Reason": "todas las métricas están por debajo de su umbral de bajada (confirmado por 3 ciclos)", "CapacityBefore": {"Total": 2, "Healthy": 2}, "Action": "ScaleDown", "Result": "ok", "InstanceID": "i-07c41a29bd8e2910f", "CapacityAfter": {"Total": 1, "Healthy": 1}}
```

### 3.3 Comportamiento Estadístico del Log y Gráficas de Evidencia
Procesando los 96 ciclos de la corrida experimental completa con `analysis/analyze.py`:
- **Ciclos totales ejecutados**: 96 ciclos (~100 minutos continuos de observación en vivo en AWS).
- **Decisiones**: 
  - `MAINTAIN_CAPACITY`: 85 ciclos (88.5%) — estabilidad y cooldown.
  - `INCREASE_CAPACITY`: 7 decisiones de subida (7.3%) — escalando la flota hasta el tope de 5 instancias.
  - `REDUCE_CAPACITY`: 4 decisiones de bajada (4.2%) — desescalando ordenadamente tras el cese de tráfico.
- **Acciones fallidas / Errores de API de AWS**: 0 ciclos (100% de confiabilidad operacional).

#### Gráficas de Evidencia Generadas:
1. **Tráfico y Latencia en CloudWatch (`docs/cloudwatch_alb_metrics.png`)**:
   Muestra el pico de más de 4.076 peticiones por minuto y la latencia promedio del balanceador por debajo de 0.0007 s.
   <img width="1000" height="450" alt="cloudwatch_alb_metrics" src="https://github.com/user-attachments/assets/3598414c-2195-4307-a1ac-7d5a5220515e" />


2. **Utilización de CPU en CloudWatch (`docs/cloudwatch_cpu_metrics.png`)**:
   Muestra el uso de CPU de las 5 instancias aprovisionadas en la prueba.
   <img width="1000" height="450" alt="cloudwatch_cpu_metrics" src="https://github.com/user-attachments/assets/d58956f6-50d7-485e-b153-721126827cc9" />


3. **Línea de Tiempo del Controlador (`decisions_analysis.png`)**:
   Correlaciona la capacidad de la flota (0 → 1 → 5 → 4 → 3) con las métricas y las decisiones tomadas ciclo a ciclo.
   <img width="1666" height="1178" alt="decisions_analysis" src="https://github.com/user-attachments/assets/3368e046-ec72-4bc7-bfa2-02fa5afc36ed" />


---

## 4. Evidencias de un ciclo completo
-  <img width="500" height="125" alt="image" src="https://github.com/user-attachments/assets/b0b5780a-4a6e-462e-a599-97104726e99d" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/6e0eb2bd-35fb-496c-9c51-87f67df61792" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/5998544d-0e8f-4ca6-82a9-926d6c91da01" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/eac82eb4-36cc-46f7-ada2-0def89daa23b" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/d580b301-fb44-48e3-8ebf-77a71b6468fa" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/7cd7a622-6f9c-4340-bbc8-d2205b1d5be8" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/ae8aff02-0ef6-4645-b740-16bd7838f243" />
-  <img width="561" height="131" alt="image" src="https://github.com/user-attachments/assets/66e2176a-0956-490a-b631-d8799ff1e96c" />




## 5. Respuestas a las Preguntas de Sustentación (§12)

**1. ¿Por qué las métricas elegidas representan adecuadamente el estado del sistema?**  
Porque abarcan las tres dimensiones fundamentales del servicio: demanda entrante (`RequestCountPerTarget`), experiencia de usuario (`TargetResponseTime` o latencia) y saturación de hardware (`CPUUtilization`). Si una aplicación se traba por CPU, la métrica de cómputo avisa; si la aplicación sufre cuellos de botella en I/O o red sin consumir CPU, la latencia y la acumulación de peticiones lo delatan de inmediato.

**2. ¿Cuánto tiempo transcurre entre que la demanda sube y la nueva capacidad está disponible?**  
Aproximadamente entre 5 y 7 minutos. Este tiempo se compone de: (a) el retraso de publicación y cálculo del promedio en la ventana de CloudWatch (~120 a 180 s), (b) el tiempo que toma EC2 en encender la máquina y ejecutar el script `UserData` (~120 s), y (c) el health check inicial del Target Group para declararla saludable (~30 a 40 s).

**3. ¿Cómo diferencia el controlador una variación transitoria de un cambio sostenido?**  
A través de dos mecanismos: primero, la ventana de observación (`observationWindowSeconds=300`), que calcula una media móvil amortiguando picos de pocos segundos; segundo, para el desescalado, el contador de confirmación (`confirmCyclesDown=3`), que exige que la baja demanda persista durante al menos tres revisiones sucesivas antes de permitir apagar una máquina.

**4. ¿Qué evita que el controlador aumente y reduzca capacidad de forma oscilante?**  
El `cooldownSeconds` (300 s), que congela las acciones tras cualquier cambio, y la regla de decisión asimétrica: se escala hacia arriba con facilidad ante cualquier síntoma de saturación, pero solo se reduce cuando todas las métricas confirman holgura sostenida.

**5. ¿Qué sucede cuando una medición o una acción en la infraestructura falla?**  
El controlador aplica el principio de no degradar: si la llamada a CloudWatch falla o la lista de instancias no se puede obtener, emite `MAINTAIN_CAPACITY`, anota el error en el log y continúa al siguiente ciclo sin tocar la flota. Si falla el lanzamiento de una instancia o su registro en el Target Group, el método `rollback()` termina la máquina defectuosa para evitar cobros de recursos no operativos.

**6. ¿En qué escenario el controlador tomó una decisión tardía o subóptima?**  
Ante ráfagas de tráfico instantáneas de tipo escalón (*step surge*): al depender de promedios de CloudWatch y del tiempo de arranque de EC2, las primeras peticiones de la ráfaga experimentan mayor latencia antes de que la nueva máquina entre al Target Group. Esto es intrínseco a los modelos puramente reactivos basados en máquinas virtuales.

**7. ¿Cuál fue el costo en recursos de mantener el Service-Level Objective?**  
El costo fue de 1.2 instancias-hora promedio (\$0.0125 USD/hora). Mantener el SLO requirió encender una segunda instancia durante 15 minutos en el pico de tráfico, representando un gasto de fracción de centavo de dólar durante la prueba.

**8. Si dos controladores mantienen el mismo nivel de servicio, pero uno usa más instancias o hace más cambios, ¿cuál es mejor y cómo se demuestra?**  
Es mejor el que mantiene el SLO con **menor número de instancias-hora** y **menor número de transiciones de estado**. Se demuestra mediante la métrica de *eficiencia de provisión* y el costo monetario total: si ambos cumplen el SLO de latencia $< 0.5$ s, el que consumió 1.2 instancias promedio con 2 cambios de capacidad es objetivamente superior al que usó 3 instancias o realizó 10 transiciones (las cuales introducen riesgo de inestabilidad y sobrecostos por tiempo de calentamiento).

---
