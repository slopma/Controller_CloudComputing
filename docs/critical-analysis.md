# Análisis Crítico del Controller de Escalado

**Asignatura:** SI3016 — Cloud Computing  
**Challenge:** 1 — Auto-Scaling Controller  
**Entregable:** 10.4 — Critical Analysis

---

## 1. Resumen del Comportamiento Observado

> ⚠️ **Nota**: Esta sección se completará con los datos reales del experimento tras ejecutarlo en AWS Academy. La estructura y los indicadores a analizar están documentados a continuación.

Durante el experimento se observaron las siguientes fases:

| Fase | Carga generada | Decisiones observadas | Capacidad |
|------|---------------|----------------------|-----------|
| Baseline (5 min) | ~10 req/s | MAINTAIN_CAPACITY | 1 instancia |
| Alta demanda (10 min) | ~50–100 req/s | INCREASE_CAPACITY | 1 → N instancias |
| Carga sostenida | Alta | MAINTAIN_CAPACITY | N instancias |
| Descenso (15 min) | ~5 req/s | REDUCE_CAPACITY | N → 1 instancia |

*Los valores exactos de N, tiempos de respuesta y número de decisiones se completarán con el log real.*

---

## 2. Fortalezas de la Política de Escalado

### 2.1 Robustez ante información incompleta
El controller nunca actúa cuando no puede determinar el estado de la flota (`Instances()` falla). Si las métricas fallan pero la flota se puede leer, mantiene la capacidad en lugar de tomar decisiones ciegas. Esta conservación deliberada del estado actual protege la disponibilidad de la aplicación.

### 2.2 Histéresis temporal con `confirmCyclesDown`
La versión mejorada requiere N ciclos consecutivos de baja carga antes de reducir capacidad. Esto evita el comportamiento de "diente de sierra" (*flapping*) donde el controller oscila entre ScaleDown y ScaleUp en respuesta a variaciones cortas de carga. Con `confirmCyclesDown=3` y `pollIntervalSeconds=60`, se requieren 3 minutos de carga baja sostenida antes de reducir.

### 2.3 Decisión asimétrica (anyAboveUp vs. allBelowDown)
- Para **subir**: basta con que **una** métrica supere el umbral → favorece el rendimiento
- Para **bajar**: **todas** las métricas deben estar bajo el umbral → favorece la disponibilidad

Esta asimetría consciente reduce el riesgo de bajo rendimiento a costa de un mayor uso de recursos. Es la decisión correcta para una aplicación de cara al usuario.

### 2.4 Restauración automática del mínimo
Si por alguna razón el número de instancias cae por debajo del mínimo (p. ej. una instancia terminada externamente), el controller actúa para restaurarlo **sin importar el cooldown ni las métricas**. Esto garantiza la disponibilidad mínima configurada incluso en situaciones inesperadas.

### 2.5 Rollback en ScaleUp fallido
Si la instancia se lanza pero no puede registrarse en el Target Group, el controller la termina automáticamente (`rollback()`). Esto evita tener capacidad "huérfana" que consuma dinero sin servir tráfico.

---

## 3. Debilidades y Limitaciones

### 3.1 Latencia de respuesta a picos súbitos
**Problema**: Con `observationWindowSeconds=300s` y `confirmCyclesDown=3`, el tiempo mínimo entre un pico de carga y la primera instancia disponible puede ser:
- 5 min (ventana de observación) + ~3 min (arranque de instancia) = ~8 min

En ese lapso, los usuarios pueden experimentar degradación del servicio.

**Mitigación posible**: Reducir `observationWindowSeconds` a 120s y `pollIntervalSeconds` a 30s para entornos con SLOs estrictos. Esto aumenta el costo de las consultas a CloudWatch.

### 3.2 El ScaleUp bloquea el ciclo
Mientras el waiter de EC2 espera que la instancia pase a `running` (hasta 3 minutos), el proceso no puede procesar nuevos ticks. Si la carga sigue aumentando durante ese tiempo, el controller no puede reaccionar.

**Mitigación posible**: Mover el `ScaleUp` a una goroutine separada con un canal de resultados, aunque esto complica el manejo de estado.

### 3.3 Cooldown único para ScaleUp y ScaleDown
El `cooldownSeconds` se aplica igual a ambas direcciones. Sería más flexible tener `cooldownUpSeconds` y `cooldownDownSeconds` independientes: reaccionar rápido a la alta carga (cooldown corto) pero ser conservador para reducir (cooldown largo).

### 3.4 Política de un solo umbral por métrica
Los umbrales son absolutos. Para métricas como `RequestCountPerTarget`, el umbral óptimo puede variar según la hora del día (tráfico nocturno vs. horario pico). Un sistema más sofisticado usaría umbrales dinámicos o bandas de confianza.

### 3.5 Estado del cooldown solo parcialmente persistido
La versión mejorada persiste `lastActionAt` en `StateFile`, pero `confirmCount` no se persiste. Tras un reinicio, el contador de ciclos de confirmación se reinicia a 0, lo que podría retrasar un ScaleDown previsto.

### 3.6 Sin coordinación multi-instancia
Si se ejecutan dos instancias del controller apuntando al mismo Target Group, tomarán decisiones contradictorias. El README advierte de esto, pero no hay mecanismo de locking implementado.

---

## 4. Cumplimiento del SLO

El objetivo de nivel de servicio (SLO) implícito es mantener `TargetResponseTime < 0.5s` (threshold de subida configurado en `conf.example.json`).

| Métrica de cumplimiento | Esperado | Observado (*completar*) |
|------------------------|----------|------------------------|
| % de tiempo con latencia < 0.5s | > 95% | — |
| Tiempo máximo de degradación por pico | < 10 min | — |
| Número de instancias-hora totales | — | — |
| Número de cambios de capacidad | — | — |

*Completar con datos del experimento.*

---

## 5. Costo de Capacidad

### Cálculo de eficiencia

Con `minInstances=1` y `maxInstances=5` (t3.micro en us-east-1 ≈ $0.0104/hora):

| Escenario | Instancias promedio | Costo/hora |
|-----------|--------------------|-----------:|
| Sin controller (siempre 5) | 5 | $0.052 |
| Con controller (experimento) | *N_prom* | *N_prom × $0.0104* |
| Ahorro estimado | — | — |

Un controller que mantiene el SLO con menos instancias que un sistema sobreaprovisionado justifica su existencia técnica y económicamente.

### Métrica de eficiencia propuesta

**Instancias-hora necesarias vs. instancias-hora usadas**:
```
Eficiencia = (instancias necesarias × tiempo) / (instancias activas × tiempo)
```

Un valor cercano a 1.0 indica que el controller usa exactamente la capacidad necesaria.

---

## 6. Comparación con la Versión Original

| Aspecto | Versión original (2025) | Versión mejorada (2026) |
|---------|------------------------|------------------------|
| Histéresis temporal | ❌ No | ✅ `confirmCyclesDown` |
| Persistencia de cooldown | ❌ No | ✅ `StateFile` |
| Paso de escala | Fijo (1) | Configurable (1 o 2) |
| Endpoint de monitoreo | ❌ No | ✅ `/health`, `/status` |
| `CapacityAfter` en log | ❌ No | ✅ Sí |
| CycleID en log | ❌ No | ✅ Sí |
| Script de carga | ❌ No | ✅ `infra/load.sh` |
| Script de análisis | ❌ No | ✅ `analysis/analyze.py` |
| Análisis taxonómico | ❌ No | ✅ `docs/taxonomy.md` |
| Tests para nuevas features | — | ✅ +9 tests |

---

## 7. Posibles Mejoras Futuras

1. **Cooldown por dirección**: `cooldownUpSeconds` y `cooldownDownSeconds` independientes
2. **ScaleUp asíncrono**: no bloquear el ciclo mientras espera el arranque de la instancia
3. **Umbrales dinámicos**: ajustar umbrales por hora del día o día de la semana
4. **Predicción**: usar regresión lineal sobre los últimos N datapoints para anticipar tendencias
5. **Alta disponibilidad del controller**: coordinación con DynamoDB para elección de líder
6. **Métricas de eficiencia**: exportar `efficiency_ratio` a CloudWatch para monitoreo externo
7. **Alertas**: enviar notificación (SNS) cuando se alcanza el máximo de instancias

---

## 8. Respuestas a las Preguntas del PDF (§12)

| Pregunta | Respuesta |
|----------|-----------|
| **1. ¿Por qué las métricas seleccionadas representan el estado?** | `RequestCountPerTarget` mide demanda directa; `TargetResponseTime` mide el impacto en el usuario (SLO); `CPUUtilization` mide saturación del recurso de cómputo. Juntas cubren demanda, impacto y capacidad. Limitación: no capturan latencia de base de datos ni errores de red. |
| **2. ¿Cuánto tiempo entre overload y capacidad disponible?** | Aproximadamente: `pollIntervalSeconds` (hasta 60s para detectar) + `observationWindowSeconds` (300s promedio) + arranque EC2 (~90-180s) + warm-up del Target Group (health check: ~20s). Total estimado: **7-10 minutos**. |
| **3. ¿Cómo distingue variación transiente de cambio sostenido?** | La ventana de observación de 300s promedia múltiples datapoints. Para scale-down, `confirmCyclesDown=3` exige 3 ciclos consecutivos (3 min) de baja carga. Para scale-up, la reacción es inmediata (sin confirmación) porque el impacto de retrasar un scale-up es mayor. |
| **4. ¿Qué previene oscilaciones?** | Dos mecanismos complementarios: (a) `cooldownSeconds=300` impide actuar inmediatamente tras una acción; (b) `confirmCyclesDown=3` requiere confirmación prolongada para scale-down. La asimetría `anyAboveUp`/`allBelowDown` también ayuda. |
| **5. ¿Qué pasa si falla una medición o acción?** | Fallo de `Instances()` → MAINTAIN sin actuar. Fallo de métricas → MAINTAIN (métricas no disponibles). Fallo de `ScaleUp` → rollback de instancia + registro de error en log. Fallo de `ScaleDown` → registro de error, la instancia queda activa (seguro). |
| **6. ¿En qué escenario hizo el controller una decisión incorrecta o tardía?** | *Completar con observación real del experimento.* Escenario esperado: si un pico de carga dura menos de `observationWindowSeconds`, la ventana de promedio lo puede "diluir" y el controller no reacciona. |
| **7. ¿Cuál fue el costo de mantener el SLO?** | *Completar con datos del experimento: instancias-hora usadas × precio por instancia.* |
| **8. ¿Qué hace mejor a un controller si mantiene el mismo SLO?** | Menor número de instancias-hora usadas, menos cambios de capacidad (estabilidad), y menor latencia de respuesta a cambios de demanda. Un controller que usa 1.5 instancias promedio vs. uno que usa 3 para el mismo SLO es objetivamente mejor desde el punto de vista de costo. Se puede demostrar calculando el `efficiency_ratio` sobre el log de decisiones. |
