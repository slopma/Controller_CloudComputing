# Clasificación Taxonómica del Controller de Escalado

**Asignatura:** SI3016 — Cloud Computing  
**Challenge:** 1 — Auto-Scaling Controller  
**Referencia:** Al-Dhuraibi et al., "Elasticity in Cloud Computing: State of the Art and Research Challenges," *IEEE Transactions on Services Computing*, vol. 11, no. 2, 2018.

---

## 1. Propósito de este Documento

El PDF del challenge (Sección 5) exige clasificar la solución implementada según la taxonomía de elasticidad propuesta por Al-Dhuraibi et al. Este documento identifica y justifica cada dimensión, distinguiendo cuáles fueron impuestas por el enunciado y cuáles corresponden a decisiones de diseño propias.

---

## 2. Taxonomía Aplicada

### 2.1 Tipo de Elasticidad

| Dimensión | Valor | Justificación |
|-----------|-------|--------------|
| **Tipo** | Horizontal (*horizontal scaling*) | Se añaden o retiran instancias EC2 completas. El tamaño de cada instancia no cambia. |
| **Dirección** | Bidireccional (escalado hacia arriba y hacia abajo) | El controller implementa las tres decisiones: `INCREASE_CAPACITY`, `REDUCE_CAPACITY` y `MAINTAIN_CAPACITY`. Ambas direcciones son necesarias para equilibrar rendimiento y costo. |

**¿Impuesta o diseño propio?**  
El enunciado impone el escalado horizontal (instancias EC2) y la bidireccionalidad ("*The solution must respond both to increases and decreases in demand*", §7). El escalado vertical no fue considerado porque requeriría detener y rearrancar las instancias, introduciendo indisponibilidad temporal inaceptable para la aplicación.

---

### 2.2 Recursos Escalados

| Recurso | Unidad | Detalle |
|---------|--------|---------|
| **Compute** | Instancias EC2 (t3.micro) | Cada instancia añade vCPU y RAM proporcionales al tipo elegido. |
| **Red (implícita)** | No gestionada directamente | La capacidad de red aumenta con las instancias pero no se controla explícitamente. |
| **Almacenamiento** | No escalado | Cada instancia tiene su propio disco efímero; el estado de la aplicación es stateless. |

**¿Impuesta o diseño propio?**  
El tipo de instancia (t3.micro) y el límite de 1–5 instancias son restricciones del enunciado (§7). La elección de t3.micro dentro de AWS Academy obedece a disponibilidad y costo de créditos de la cuenta sandbox.

---

### 2.3 Alcance del Mecanismo (*Scope*)

| Nivel | Valor | Justificación |
|-------|-------|--------------|
| **Alcance de aplicación** | Aplicación única | El controller gestiona una sola aplicación (servidor web Nginx) detrás de un único ALB y Target Group. |
| **Alcance de recursos** | Instancias gestionadas por tag | Solo las instancias con el tag `managed-by=controllerasg` son consideradas por la flota. Esto aisla la flota gestionada de otras instancias en la cuenta. |
| **Alcance geográfico** | Región única (us-east-1) | No hay distribución multi-región. Las subredes se distribuyen en múltiples zonas de disponibilidad dentro de la región para tolerancia a fallos de AZ. |

**¿Impuesta o diseño propio?**  
El enunciado implica una aplicación única (§6). La restricción a una región es una simplificación de diseño justificada por el alcance educativo del challenge. El uso de múltiples subredes/AZ es una decisión propia para mejorar la disponibilidad.

---

### 2.4 Propósito

| Objetivo | Descripción |
|----------|-------------|
| **Rendimiento** | Mantener la latencia de respuesta por debajo del umbral configurado (`TargetResponseTime < 0.5s`). |
| **Costo** | Liberar instancias cuando la carga cae, minimizando el número de instancias activas y el costo por instancia-hora. |
| **Disponibilidad** | Garantizar al menos 1 instancia activa (mínimo configurado) y restaurar la capacidad si cae por debajo del mínimo, incluso sin datos de métricas. |

**Balance rendimiento-costo:**  
El controller usa un umbral de escala-arriba conservador (evitar actuar si hay métricas faltantes) y un mecanismo de confirmación temporal para escala-abajo (`confirmCyclesDown`), reduciendo la frecuencia de cambios de capacidad y sus costos asociados.

---

### 2.5 Modo de Operación

| Dimensión | Valor | Detalle |
|-----------|-------|---------|
| **Modo** | Reactivo (*reactive*) | El controller responde a métricas observadas del estado actual del sistema; no predice demanda futura. |
| **Tipo de trigger** | Basado en umbrales (*threshold-based*) | Las métricas se comparan contra `scaleUpThreshold` y `scaleDownThreshold` por cada política configurada. |
| **Ventana temporal** | Promedio en ventana deslizante | Se promedian los datapoints de CloudWatch dentro de `observationWindowSeconds` (por defecto 300 s), atenuando picos momentáneos. |

**¿Por qué reactivo y no proactivo?**  
Un sistema proactivo requiere modelos de predicción de carga (p. ej. series temporales o ML). Dado el alcance del challenge y la variabilidad arbitraria de la carga generada, el enfoque reactivo es más robusto y explicable. La histéresis temporal (`confirmCyclesDown`) añade una forma ligera de filtrado temporal sin necesidad de predicción.

---

### 2.6 Método de Toma de Decisiones

| Dimensión | Valor | Justificación |
|-----------|-------|--------------|
| **Método** | Basado en reglas (*rule-based*) con umbrales | Las condiciones de escala son reglas explícitas, auditables y deterministas. |
| **Función de decisión** | Mayoría de métricas para subir, unanimidad para bajar | `anyAboveUp` (basta con que una métrica supere el umbral) para INCREASE; `allBelowDown` (todas deben estar bajo el umbral) para REDUCE. |
| **Anti-oscilación** | Cooldown + histéresis por ciclos | El `cooldownSeconds` impide actuar inmediatamente después de una acción. El `confirmCyclesDown` requiere N ciclos consecutivos de baja carga antes de reducir. |

**Justificación del diseño asimétrico de decisión:**  
Se elige `anyAboveUp` (basta una métrica alta) porque una sola dimensión saturada (CPU o requests) ya pone en riesgo el SLO. Para reducir se exige `allBelowDown` porque si alguna métrica sigue en zona de trabajo es señal de que la capacidad actual es necesaria. Este diseño favorece disponibilidad sobre costo, lo cual es apropiado para una aplicación de cara al usuario.

---

### 2.7 Arquitectura del Controller

| Dimensión | Valor | Justificación |
|-----------|-------|--------------|
| **Topología** | Centralizado | Un único proceso `controllerasg` toma todas las decisiones. |
| **Estilo de control** | Bucle cerrado (*closed-loop*) | Observe → Decide → Act → (repite). Cada ciclo lee el estado actual antes de decidir. |
| **Separación de responsabilidades** | Observer / Controller / Provisioner | El `observer` consulta CloudWatch, el `controller` aplica la política, el `provisioner` ejecuta las acciones en EC2/ELB. |
| **Estado interno** | Mínimo | Solo `lastActionAt`, `cycleID` y `confirmCount`. El estado de la flota se lee en cada ciclo desde AWS. |

**¿Impuesta o diseño propio?**  
El estilo de bucle cerrado es la forma natural de implementar un controller autónomo. La separación en paquetes es una decisión de diseño para facilitar la prueba unitaria mediante interfaces y mocks.

**Limitación reconocida:**  
La arquitectura centralizada crea un *single point of failure*: si el proceso del controller cae, el sistema no escala hasta que se reinicia. Para producción sería necesario un mecanismo de elección de líder (p. ej. DynamoDB + TTL) o un servicio gestionado.

---

### 2.8 Alcance de Proveedor Cloud

| Dimensión | Valor | Detalle |
|-----------|-------|---------|
| **Proveedor** | Single-cloud (AWS exclusivo) | Todos los componentes son servicios AWS: EC2, ELBv2, CloudWatch. |
| **Portabilidad** | Baja | El código usa el SDK de AWS Go v2; migrar a otro proveedor requeriría reimplementar `observer` y `provisioner`. |
| **Servicios propios usados** | EC2, ALB, CloudWatch | No se usan servicios de escalado gestionado de AWS (Auto Scaling Groups con políticas dinámicas). |

**¿Impuesta o diseño propio?**  
El uso de AWS es impuesto por el enunciado (§7). La restricción de no usar AWS Auto Scaling Policies ("*AWS-managed dynamic scaling policies may not be used*", §7.4) es explícita y justifica la existencia del controller propio.

---

## 3. Dimensiones Impuestas vs. Decisiones Propias

| Dimensión | ¿Impuesta? | Alternativas no elegidas |
|-----------|-----------|--------------------------|
| Escalado horizontal | ✅ Impuesta | Escalado vertical (no permitido por EC2 sin reinicio) |
| Bidireccional | ✅ Impuesta | Solo scale-up (no cubriría reducción de costo) |
| Min=1, Max=5 | ✅ Impuesta | — |
| Uso de CloudWatch | ✅ Impuesta | Métricas propias vía agente (mayor latencia) |
| Sin AWS ASG policies | ✅ Impuesta | — |
| Modo reactivo | ⚙️ Diseño propio | Proactivo con predicción de series temporales |
| `anyAboveUp` / `allBelowDown` | ⚙️ Diseño propio | Media ponderada, votación por mayoría |
| `confirmCyclesDown` (histéresis) | ⚙️ Diseño propio | Sin histéresis (mayor riesgo de oscilación) |
| Arquitectura centralizada | ⚙️ Diseño propio | Distribuida con elección de líder |
| Paso de escala configurable (1 o 2) | ⚙️ Diseño propio | Solo paso 1 (menos agresivo) |

---

## 4. Relación con la Taxonomía de Al-Dhuraibi et al.

Según la Figura 2 de Al-Dhuraibi et al., los principales ejes de clasificación son:

| Eje del paper | Clasificación de esta solución |
|---------------|-------------------------------|
| *Elasticity type* | Horizontal |
| *Elasticity direction* | Bidirectional |
| *Scaled resource* | Compute (VMs) |
| *Scope* | Single application |
| *Purpose* | Performance + Cost |
| *Operation mode* | Reactive |
| *Decision method* | Rule-based (threshold) |
| *Controller architecture* | Centralized |
| *Cloud provider scope* | Single provider (AWS) |

Esta clasificación corresponde a lo que los autores denominan un sistema de elasticidad **reactivo basado en reglas de umbral**, que es el tipo más común en entornos de producción por su explicabilidad y bajo costo computacional, aunque tiene como limitación principal la dificultad para responder a cambios de demanda antes de que afecten el rendimiento.
