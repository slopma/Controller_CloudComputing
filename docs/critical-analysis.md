# Analisis Critico del Controller de Escalado

**Materia:** SI3016 — Computacion en la Nube  
**Reto:** 1 — Auto-Scaling Controller  
**Entregable:** 10.4 — Analisis Critico

---

## 1. Resumen del Comportamiento Observado

Durante la prueba del experimento se simularon las siguientes fases de carga:

| Fase | Carga generada | Decisiones esperadas/vistas | Capacidad |
|------|---------------|----------------------|-----------|
| Carga inicial (5 min) | ~10 req/s | MAINTAIN_CAPACITY | 1 instancia |
| Alta demanda (10 min) | ~50–100 req/s | INCREASE_CAPACITY | 1 → N instancias |
| Carga sostenida | Alta | MAINTAIN_CAPACITY | N instancias |
| Descenso (15 min) | ~5 req/s | REDUCE_CAPACITY | N → 1 instancia |

*(Nota: los valores finales de N y tiempos exactos se llenan con la corrida real en AWS).*

---

## 2. Puntos fuertes de la logica implementada

### 2.1 No actua si no sabe como esta la flota
Si la llamada a `DescribeInstances` falla o no se puede leer la flota, el controller prefiere quedarse quieto y poner `MAINTAIN_CAPACITY`. Esto es bueno porque tomar decisiones a ciegas podria tumbar instancias necesarias por error.

### 2.2 Histeresis con `confirmCyclesDown`
Para apagar maquinas no basta con que la carga baje un solo ciclo. El controller pide que la carga este baja durante N ciclos seguidos (por defecto 3 ciclos, o sea 3 minutos). Esto evita el famoso problema del "flapping" o efecto serrucho, donde el sistema se la pasa prendiendo y apagando instancias por cualquier pico chiquito.

### 2.3 Asimetria al decidir (subir rapido, bajar lento)
- Para **subir**: basta con que **una sola** metrica pase el umbral alto (para proteger el servicio rapido).
- Para **bajar**: **todas** las metricas tienen que estar por debajo del umbral bajo (para estar bien seguros de que ya no se necesita la maquina).

### 2.4 Mantiene el piso minimo de instancias
Si por alguna razon una maquina se muere o la apagan manualmente y quedamos por debajo de `minInstances`, el script manda a prender una instancia de inmediato, ignorando cooldown y metricas.

### 2.5 Rollback si falla el arranque
Si al mandar a crear la instancia en EC2 o al registrarla en el target group de AWS ocurre algun error, el controller llama a `rollback()` para matarla y que no quede cobrando dinero sin recibir trafico.

---

## 3. Limitaciones encontradas

### 3.1 Demora al reaccionar a picos muy rapidos
Entre la ventana de observacion (300s en CloudWatch) y lo que se demora EC2 en prender la maquina y que pase el healthcheck del balanceador (unos 2 o 3 minutos), una instancia nueva puede tardar entre 6 y 8 minutos en estar lista. Si el trafico sube de golpe, los usuarios pueden sentir lentitud mientras sube la maquina.

### 3.2 El ScaleUp bloquea el bucle
Como el script espera a que la instancia pase a estado `running` antes de continuar, durante ese par de minutos el bucle principal se queda bloqueado esperando. Si quisieramos mejorarlo podriamos mandar el waiter a un hilo secundario (`threading.Thread`), pero complica mas el codigo.

### 3.3 Mismo cooldown para subir y bajar
El `cooldownSeconds` es el mismo tanto para subida como para bajada. Lo ideal seria tener un cooldown mas corto para prender maquinas y uno mas largo para apagarlas.

### 3.4 Umbrales fijos
Los valores de los umbrales en `conf.json` son estaticos. No cambian si es de noche o de dia.

---

## 4. Revision del SLO

El objetivo de servicio que se busca es que `TargetResponseTime` no pase de 0.5s en promedio.

| Indicador | Esperado | Observado |
|-----------|----------|-----------|
| % tiempo con latencia < 0.5s | > 95% | (completar con log) |
| Tiempo de recuperacion ante pico | < 10 min | (completar con log) |
| Total instancias-hora | — | (completar con log) |

---

## 5. Costo y Eficiencia

Teniendo instancias t3.micro (costo aprox $0.0104 la hora):
- Sin controller (dejando siempre 5 maquinas prendidas): $0.052 por hora.
- Con controller: promedio entre 1 y 3 maquinas, ahorrando mas del 40-50% del costo total.

Formula basica de eficiencia:
```
Eficiencia = (instancias necesarias × tiempo) / (instancias activas × tiempo)
```

---

## 6. Funcionalidades implementadas en el controlador

| Caracteristica | Como funciona en el codigo | Para que sirve |
|----------------|----------------------------|----------------|
| Histeresis temporal | Con `confirmCyclesDown` (cuenta ciclos seguidos con carga baja) | Evita que se apaguen maquinas por caidas de trafico muy cortas |
| Cooldown persistente | Guarda la hora de la ultima accion en `stateFile` | Si se reinicia el script, no arranca a escalar a lo loco |
| Pasos configurables | Variables `scaleUpStep` y `scaleDownStep` | Permite prender o apagar de a 1 o 2 maquinas por decision |
| Endpoint HTTP | Servidor con `http.server` en `statusPort` | Permite consultar `/health` y `/status` facilmente |
| Log con detalle | Guarda `CapacityBefore` y `CapacityAfter` en cada linea | Para saber cuantas maquinas habia antes y despues de actuar |
| Contador de ciclos | `CycleID` incremental | Facilita ordenar y leer los eventos en el archivo `decisions.jsonl` |
| Pruebas de carga | Script `infra/load.sh` | Para probar el controller con diferentes niveles de trafico |
| Analisis de resultados | Script `analysis/analyze.py` | Lee el jsonl y saca promedios y graficas |
| Tests automatizados | Suite con `pytest` en `tests/` | Para verificar que toda la logica funcione bien |

---

## 7. Preguntas de sustentacion (§12)

**1. ¿Por que las metricas elegidas representan el estado del sistema?**  
Porque cubren los tres frentes: `RequestCountPerTarget` mide la demanda que llega, `TargetResponseTime` mide como lo siente el usuario (latencia), y `CPUUtilization` mide el uso de la maquina. Lo que no miden es si hay errores internos de base de datos o caidas de red externas.

**2. ¿Cuanto tiempo pasa entre la sobrecarga y tener la capacidad lista?**  
Aprox entre 6 y 9 minutos: lo que tarda CloudWatch en reflejar los datos segun la ventana (3-5 min), mas el arranque de la maquina EC2 (~2 min) y el health check del ALB (~30s).

**3. ¿Como diferencia una variacion transitoria de un cambio sostenido?**  
Con la ventana de tiempo de 300 segundos que promedia los valores, y ademas con `confirmCyclesDown=3`, que exige que la carga se mantenga baja por 3 ciclos consecutivos antes de decidir apagar una maquina.

**4. ¿Que evita que el controller se ponga a oscilar (subir y bajar)?**  
El `cooldownSeconds` (espera 5 minutos despues de cada accion) y la histeresis de los 3 ciclos seguidos para reducir. Ademas de la regla asimetrica: para subir basta una metrica alta, pero para bajar todas tienen que estar bajas.

**5. ¿Que pasa si falla AWS o una medicion?**  
Si falla leer la flota o las metricas, el script se queda en `MAINTAIN_CAPACITY` y anota el error en el log. Si falla al prender una instancia, hace rollback para no dejarla viva. Si falla al apagar, anota el error y la maquina sigue prendida para no afectar el servicio.

**6. ¿Cual fue el costo de mantener el SLO?**  
Se calcula multiplicando las instancias promedio usadas por el costo horario de la t3.micro durante el tiempo que dura la prueba.

**7. Si dos controladores mantienen el mismo SLO, ¿cual es mejor?**  
El que gaste menos instancias-hora y haga menos cambios bruscos de capacidad. El que logre el objetivo con 1.5 maquinas promedio es mejor que uno que gaste 3 maquinas para lo mismo, porque ahorra costos manteniendo la misma calidad de servicio.
