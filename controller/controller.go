package controller

import (
	"context"
	"encoding/json"
	"log"
	"os"
	"sync"
	"time"

	"controllerasg/configuration"
	"controllerasg/model"
)

// MetricsSource observa el estado de la aplicación. Recibe la flota actual
// porque algunas métricas (p. ej. CPU) se consultan por instancia.
type MetricsSource interface {
	Observe(ctx context.Context, fleet []model.Instance) ([]model.ObservedMetric, error)
}

// Fleet informa de las instancias gestionadas y su salud: es la fuente de verdad
// de la capacidad.
type Fleet interface {
	Instances(ctx context.Context) ([]model.Instance, error)
}

// Actuator devuelve el id de la instancia creada o retirada.
type Actuator interface {
	ScaleUp(ctx context.Context) (string, error)
	ScaleDown(ctx context.Context) (string, error)
}

type Logger interface {
	Log(rec model.DecisionRecord)
}

// persistedState es el estado que se guarda en disco para sobrevivir reinicios.
type persistedState struct {
	LastActionAt time.Time `json:"lastActionAt"`
}

// Controller implementa el bucle cerrado observar → decidir → actuar.
//
// Mejoras respecto a la versión original:
//   - confirmCyclesDown: histéresis temporal; requiere N ciclos consecutivos de
//     baja carga antes de reducir, evitando ScaleDown por caídas transitorias.
//   - scaleUpStep / scaleDownStep: número configurable de instancias por acción.
//   - Persistencia del cooldown: lastActionAt se guarda en disco para sobrevivir reinicios.
//   - cycleID: contador secuencial de ciclos para correlacionar registros en el log.
//   - CapacityAfter: el log recoge el estado de la flota tras ejecutar la acción.
type Controller struct {
	config       *configuration.Config
	metrics      MetricsSource
	fleet        Fleet
	actuator     Actuator
	logger       Logger
	lastActionAt time.Time
	// cycleID se incrementa en cada Tick; permite correlacionar registros.
	cycleID int
	// confirmCount acumula cuántos ciclos consecutivos todas las métricas han
	// estado bajo su umbral de bajada. Cuando llega a ConfirmCyclesDown se actúa.
	confirmCount int
	// mu protege lastActionAt, cycleID y confirmCount en entornos concurrentes.
	mu sync.Mutex
	// lastRecord guarda la última DecisionRecord para el endpoint /status.
	lastRecord   model.DecisionRecord
	lastRecordMu sync.RWMutex
}

func New(
	config *configuration.Config,
	metrics MetricsSource,
	fleet Fleet,
	actuator Actuator,
	logger Logger,
) *Controller {
	c := &Controller{
		config:   config,
		metrics:  metrics,
		fleet:    fleet,
		actuator: actuator,
		logger:   logger,
	}
	c.loadState()
	return c
}

// LastRecord devuelve el último DecisionRecord registrado (para /status HTTP).
func (c *Controller) LastRecord() model.DecisionRecord {
	c.lastRecordMu.RLock()
	defer c.lastRecordMu.RUnlock()
	return c.lastRecord
}

// Tick ejecuta un ciclo observar → decidir → actuar y registra exactamente un
// DecisionRecord, incluso cuando algo falla.
func (c *Controller) Tick(ctx context.Context) {
	c.mu.Lock()
	c.cycleID++
	cycleID := c.cycleID
	c.mu.Unlock()

	rec := model.DecisionRecord{
		CycleID:       cycleID,
		Time:          time.Now(),
		Decision:      model.Maintain,
		WindowSeconds: c.config.ObservationWindowSeconds,
		Action:        "none",
		Result:        "none",
	}
	defer func() {
		c.logger.Log(rec)
		c.lastRecordMu.Lock()
		c.lastRecord = rec
		c.lastRecordMu.Unlock()
	}()

	// --- Observar flota ---
	fleet, err := c.fleet.Instances(ctx)
	if err != nil {
		// Sin saber cuánta capacidad hay no se puede actuar con seguridad.
		rec.Reason = "no se pudo leer la flota, no se actúa"
		rec.Error = err.Error()
		return
	}
	cap := summarize(fleet)
	rec.Capacity = cap
	rec.CapacityBefore = cap

	// --- Observar métricas ---
	observed, obsErr := c.metrics.Observe(ctx, fleet)
	if obsErr != nil {
		observed = nil
		rec.Error = "observación: " + obsErr.Error()
	}
	rec.Metrics = observed

	// --- Decidir ---
	c.mu.Lock()
	decision, reason, confirmCycle := c.decide(observed, len(fleet))
	rec.ConfirmCycle = confirmCycle
	c.mu.Unlock()

	rec.Decision = decision
	rec.Reason = reason
	if obsErr != nil && rec.Decision == model.Maintain {
		rec.Reason = "métricas no disponibles, no se actúa"
	}

	// --- Actuar ---
	var actionErr error
	switch rec.Decision {
	case model.Increase:
		rec.Action = "ScaleUp"
		// scaleUpStep: lanzar hasta N instancias (limitado por el máximo).
		for i := 0; i < c.config.ScaleUpStep; i++ {
			var id string
			id, actionErr = c.actuator.ScaleUp(ctx)
			if actionErr != nil {
				break
			}
			rec.InstanceID = id // se guarda el último id lanzado
		}
	case model.Reduce:
		rec.Action = "ScaleDown"
		// scaleDownStep: retirar hasta N instancias (limitado por el mínimo).
		for i := 0; i < c.config.ScaleDownStep; i++ {
			var id string
			id, actionErr = c.actuator.ScaleDown(ctx)
			if actionErr != nil {
				break
			}
			rec.InstanceID = id
		}
	default:
		return
	}

	if actionErr != nil {
		rec.Result = "error"
		rec.Error = actionErr.Error()
		return
	}
	rec.Result = "ok"

	// Registrar el instante de acción y persistirlo.
	c.mu.Lock()
	c.lastActionAt = time.Now()
	c.mu.Unlock()
	c.saveState()

	// CapacityAfter: leer la flota real tras actuar para reflejarlo en el log.
	if afterFleet, err := c.fleet.Instances(ctx); err == nil {
		after := summarize(afterFleet)
		rec.CapacityAfter = &after
	} else {
		log.Printf("[controller] aviso: no se pudo leer la flota post-acción: %v", err)
	}
}

// decide implementa la política de escalado con histéresis temporal.
//
// Devuelve la decisión, el motivo y el ciclo de confirmación actual.
// IMPORTANTE: debe llamarse con c.mu tomado.
func (c *Controller) decide(observed []model.ObservedMetric, currentCount int) (model.Decision, string, int) {
	// Regla de seguridad prioritaria: restaurar el mínimo sin importar métricas ni cooldown.
	if currentCount < c.config.Compute.MinInstances {
		c.confirmCount = 0
		return model.Increase, "capacidad por debajo del mínimo configurado", 0
	}

	// Cooldown: no actuar si la última acción fue reciente.
	if time.Since(c.lastActionAt) < time.Duration(c.config.CooldownSeconds)*time.Second {
		return model.Maintain, "en cooldown", c.confirmCount
	}

	// Sin métricas: mantener siempre.
	if len(observed) == 0 {
		c.confirmCount = 0
		return model.Maintain, "sin datos de métricas, no se actúa", 0
	}

	values := toMap(observed)

	anyAboveUp := false
	allBelowDown := true

	for _, policy := range c.config.Metrics {
		v, ok := values[policy.MetricName]
		if !ok {
			// Una métrica sin dato se abstiene. Ver comentario en versión original.
			continue
		}
		if v >= policy.Scaling.ScaleUpThreshold {
			anyAboveUp = true
		}
		if v > policy.Scaling.ScaleDownThreshold {
			allBelowDown = false
		}
	}

	switch {
	case anyAboveUp && currentCount < c.config.Compute.MaxInstances:
		// ScaleUp no requiere confirmación de ciclos: la alta carga es urgente.
		c.confirmCount = 0
		return model.Increase, "al menos una métrica superó su umbral de subida", 0

	case allBelowDown && currentCount > c.config.Compute.MinInstances:
		// Histéresis temporal: acumular N ciclos consecutivos antes de reducir.
		c.confirmCount++
		if c.confirmCount >= c.config.ConfirmCyclesDown {
			c.confirmCount = 0
			return model.Reduce,
				"todas las métricas bajo umbral durante los ciclos de confirmación requeridos",
				c.config.ConfirmCyclesDown
		}
		return model.Maintain,
			"métricas bajo umbral, esperando confirmación de ciclos antes de reducir",
			c.confirmCount

	default:
		c.confirmCount = 0
		return model.Maintain, "condiciones estables, métricas incompletas o en límites de min/max", 0
	}
}

// --- Estado persistido (cooldown entre reinicios) ---

func (c *Controller) stateFilePath() string {
	if c.config.StateFile == "" {
		return ""
	}
	return c.config.StateFile
}

// loadState lee lastActionAt del archivo de estado si existe.
// Si el archivo no existe o hay un error, arranca sin lastActionAt (comportamiento seguro).
func (c *Controller) loadState() {
	path := c.stateFilePath()
	if path == "" {
		return
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("[controller] aviso: no se pudo leer el estado persistido (%s): %v", path, err)
		}
		return
	}
	var s persistedState
	if err := json.Unmarshal(data, &s); err != nil {
		log.Printf("[controller] aviso: estado persistido malformado, se ignora: %v", err)
		return
	}
	c.lastActionAt = s.LastActionAt
	log.Printf("[controller] cooldown restaurado: última acción en %s", c.lastActionAt.Format(time.RFC3339))
}

// saveState escribe lastActionAt al archivo de estado.
func (c *Controller) saveState() {
	path := c.stateFilePath()
	if path == "" {
		return
	}
	c.mu.Lock()
	s := persistedState{LastActionAt: c.lastActionAt}
	c.mu.Unlock()

	data, err := json.Marshal(s)
	if err != nil {
		log.Printf("[controller] error al serializar estado: %v", err)
		return
	}
	if err := os.WriteFile(path, data, 0o644); err != nil {
		log.Printf("[controller] error al guardar estado (%s): %v", path, err)
	}
}

// --- Helpers ---

func summarize(fleet []model.Instance) model.Capacity {
	cap := model.Capacity{Total: len(fleet), Instances: make([]model.InstanceStatus, 0, len(fleet))}
	for _, i := range fleet {
		if i.Health == "healthy" {
			cap.Healthy++
		}
		cap.Instances = append(cap.Instances, model.InstanceStatus{ID: i.Id, State: i.State, Health: i.Health})
	}
	return cap
}

func toMap(observed []model.ObservedMetric) map[string]float64 {
	m := make(map[string]float64, len(observed))
	for _, o := range observed {
		m[o.Name] = o.Value
	}
	return m
}
