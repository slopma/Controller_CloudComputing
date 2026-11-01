package model

import "time"

type Decision string

const (
	Maintain Decision = "MAINTAIN_CAPACITY"
	Increase Decision = "INCREASE_CAPACITY"
	Reduce   Decision = "REDUCE_CAPACITY"
)

// InstanceStatus es la vista compacta de una instancia que se guarda en el log.
type InstanceStatus struct {
	ID     string
	State  string
	Health string
}

type Capacity struct {
	Total     int
	Healthy   int
	Instances []InstanceStatus
}

// DecisionRecord es una línea del log de decisiones: permite reconstruir qué se
// observó, qué se decidió y por qué, qué se pidió a la infraestructura y qué pasó.
//
// Campos añadidos respecto a la versión original:
//   - CycleID: número secuencial de ciclo desde el arranque del proceso.
//   - CapacityBefore/CapacityAfter: capacidad antes y después de actuar, para
//     calcular el delta neto sin necesidad de correlacionar registros.
//   - ConfirmCycle: cuántos ciclos consecutivos de baja carga lleva acumulados
//     el controller (útil para entender la histéresis temporal de ScaleDown).
type DecisionRecord struct {
	// CycleID es el número de ciclo desde el arranque (1-indexed).
	CycleID int `json:"CycleID"`
	// Time es el instante en que se tomó la decisión.
	Time time.Time
	// Decision es una de las tres decisiones posibles del controller.
	Decision Decision
	// Reason explica en lenguaje natural por qué se tomó esa decisión.
	Reason string
	// WindowSeconds es la ventana de observación en segundos usada este ciclo.
	WindowSeconds int
	// Metrics son los valores observados de cada métrica configurada.
	Metrics []ObservedMetric
	// CapacityBefore es el estado de la flota al inicio del ciclo (antes de actuar).
	CapacityBefore Capacity `json:"CapacityBefore"`
	// Capacity (alias de CapacityBefore, mantenido por compatibilidad con versión anterior).
	Capacity Capacity
	// CapacityAfter es el estado de la flota tras ejecutar la acción. Solo se
	// rellena cuando se produjo un ScaleUp o ScaleDown exitoso.
	CapacityAfter *Capacity `json:"CapacityAfter,omitempty"`
	// ConfirmCycle es el número de ciclos consecutivos de baja carga acumulados.
	// Solo es relevante cuando Decision==MAINTAIN o REDUCE.
	ConfirmCycle int `json:"ConfirmCycle,omitempty"`
	// InstanceID es el ID de la instancia creada o retirada en este ciclo.
	InstanceID string
	// Action describe la acción solicitada: "ScaleUp", "ScaleDown" o "none".
	Action string
	// Result describe el resultado: "ok", "error" o "none".
	Result string
	// Error contiene el mensaje de error cuando Result=="error".
	Error string
}
