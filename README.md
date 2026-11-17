# Reto 1 - Auto-Scaling Controller (Computacion en la Nube)

Este proyecto es un controlador de auto-scaling hecho en Python para el reto 1 de la materia SI3016. La idea del programa es controlar la cantidad de instancias EC2 que estan corriendo segun la carga que haya, sin usar el auto scaling nativo de AWS sino haciendolo uno mismo con un bucle de control.

El controlador revisa metricas en CloudWatch (como la cpu o la cantidad de requests), decide si hace falta prender o apagar instancias, y las conecta o desconecta del Target Group del Application Load Balancer (ALB). Todas las decisiones que toma quedan guardadas en un archivo `decisions.jsonl` para poder revisarlas despues.

---

## Como funciona

El programa corre un ciclo cada cierto tiempo (`pollIntervalSeconds`):

1. **Revisa la flota**: mira cuantas instancias tienen el tag `managed-by=controllerasg` y en que estado estan en el target group (healthy, initial, etc).
2. **Lee las metricas**: consulta CloudWatch para ver el promedio en la ventana de observacion (`observationWindowSeconds`).
3. **Toma la decision**:
   - Si hay menos instancias que el minimo configurado (`minInstances`), sube capacidad de una vez.
   - Si todavia esta en tiempo de cooldown (`cooldownSeconds`), mantiene la capacidad para no actuar antes de tiempo.
   - Si no llegaron metricas, no hace nada para evitar errores.
   - Si alguna metrica pasa el umbral de subida -> `INCREASE_CAPACITY` (sube capacidad).
   - Si todas las metricas estan por debajo del umbral de bajada por N ciclos seguidos (`confirmCyclesDown`) -> `REDUCE_CAPACITY` (baja capacidad).
   - De lo contrario -> `MAINTAIN_CAPACITY` (se queda igual).
4. **Ejecuta la accion**:
   - Para subir: prende una maquina con el launch template, espera a que pase a `running` y la registra en el target group. Si algo falla la apaga para no dejarla huerfana.
   - Para bajar: escoge la instancia (primero las que no esten healthy o la mas nueva), la saca del target group, espera que termine de drenar conexiones y la termina.
5. **Guarda en el log**: escribe una linea en `decisions.jsonl` con lo que paso.

---

## Requisitos e Instalacion

Se necesita Python 3.10 o superior y las credenciales de AWS configuradas en la maquina (con `aws configure` o las variables de entorno de AWS Academy).

Para instalar las librerias necesarias:

```bash
pip install -r requirements.txt
```

*(Las librerias son `boto3` para conectar con AWS, `pytest` para las pruebas y `matplotlib` si se quiere graficar).*

---

## Como usarlo

### 1. Validar la configuracion

Para ver si el archivo `conf.json` esta bien escrito antes de correr nada con AWS:

```bash
python3 main.py -config conf.json -check
# o tambien:
./bin/controllerasg -config conf.json -check
```

### 2. Arrancar el controller

Para poner a rodar el controlador:

```bash
python3 main.py -config conf.json -log decisions.jsonl
# o con el script:
./bin/controllerasg -config conf.json -log decisions.jsonl
```

Para pararlo solo se usa `Ctrl + C` y se apaga de forma limpia.

### 3. Asistente interactivo (init.sh)

Hay un script `init.sh` que hace preguntas basicas por consola para armar el `conf.json` facil y arrancar el controller:

```bash
./init.sh
```

---

## Configuracion (conf.json)

El archivo `conf.json` tiene los parametros de como debe funcionar el auto-scaling. Hay un ejemplo en `conf.example.json`:

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
  ]
}
```

Parametros principales:
- `minInstances` y `maxInstances`: limites de maquinas (entre 1 y 5).
- `pollIntervalSeconds`: cada cuantos segundos corre el ciclo (ej: 60).
- `cooldownSeconds`: segundos de espera despues de prender o apagar maquinas para no oscilar (ej: 300).
- `confirmCyclesDown`: cuantos ciclos seguidos con carga baja se necesitan antes de apagar una instancia (histeresis).
- `statusPort`: puerto para un servidor http simple (endpoint `/health` y `/status`). Si se pone en 0 queda deshabilitado.
- `stateFile`: archivo donde guarda la hora del ultimo escalado para que el cooldown no se pierda si se reinicia el proceso.

---

## Log de decisiones y analisis

Cada ciclo agrega una linea a `decisions.jsonl`. Por ejemplo:

```json
{"CycleID": 5, "Time": "2026-09-22T16:01:48-05:00", "Decision": "MAINTAIN_CAPACITY", "Reason": "condiciones estables", "CapacityBefore": {"Total": 2, "Healthy": 2}, "Action": "none", "Result": "none"}
```

Para ver el resumen estadistico o generar la grafica:

```bash
python3 analysis/analyze.py decisions.jsonl
# o sin grafica:
python3 analysis/analyze.py decisions.jsonl --no-plot
```

