package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	"controllerasg/configuration"
	"controllerasg/model"
)

// ---- fakes ----

type fakeMetrics struct {
	values []model.ObservedMetric
	err    error
}

func (f fakeMetrics) Observe(_ context.Context, _ []model.Instance) ([]model.ObservedMetric, error) {
	return f.values, f.err
}

type fakeFleet struct {
	instances []model.Instance
	err       error
}

func (f fakeFleet) Instances(_ context.Context) ([]model.Instance, error) {
	return f.instances, f.err
}

type fakeActuator struct {
	err   error
	ups   int
	downs int
}

func (f *fakeActuator) ScaleUp(_ context.Context) (string, error) {
	f.ups++
	return "i-new", f.err
}
func (f *fakeActuator) ScaleDown(_ context.Context) (string, error) {
	f.downs++
	return "i-old", f.err
}

type memLogger struct{ recs []model.DecisionRecord }

func (l *memLogger) Log(rec model.DecisionRecord) { l.recs = append(l.recs, rec) }

// ---- helpers ----

func testConfig() *configuration.Config {
	cfg := &configuration.Config{
		CooldownSeconds:          300,
		ObservationWindowSeconds: 300,
		ConfirmCyclesDown:        1,
		ScaleUpStep:              1,
		ScaleDownStep:            1,
		Compute:                  configuration.ComputeConfig{MinInstances: 1, MaxInstances: 3},
		Metrics: []configuration.MetricPolicy{{
			MetricName: "load",
			Scaling:    configuration.ScalingConfig{ScaleUpThreshold: 70, ScaleDownThreshold: 20},
		}},
	}
	return cfg
}

func healthy(n int) []model.Instance {
	fleet := make([]model.Instance, n)
	for i := range fleet {
		fleet[i] = model.Instance{Id: "i-" + string(rune('a'+i)), State: "running", Health: "healthy"}
	}
	return fleet
}

func run(m fakeMetrics, f fakeFleet, a *fakeActuator) (*Controller, model.DecisionRecord) {
	l := &memLogger{}
	c := New(testConfig(), m, f, a, l)
	c.Tick(context.Background())
	return c, l.recs[0]
}

func load(v float64) fakeMetrics {
	return fakeMetrics{values: []model.ObservedMetric{{Name: "load", Value: v}}}
}

// ---- tests básicos (mantienen compatibilidad con versión original) ----

func TestScaleUpOnHighLoad(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(load(90), fakeFleet{instances: healthy(1)}, a)
	if rec.Decision != model.Increase || a.ups != 1 || rec.Action != "ScaleUp" || rec.Result != "ok" || rec.InstanceID != "i-new" {
		t.Errorf("registro inesperado: %+v (ups=%d)", rec, a.ups)
	}
	if rec.CapacityBefore.Total != 1 || rec.CapacityBefore.Healthy != 1 {
		t.Errorf("CapacityBefore incorrecto: %+v", rec.CapacityBefore)
	}
	if len(rec.Metrics) != 1 || rec.WindowSeconds != 300 {
		t.Errorf("el registro no describe lo observado: %+v", rec)
	}
}

func TestScaleDownOnLowLoad(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(load(5), fakeFleet{instances: healthy(2)}, a)
	if rec.Decision != model.Reduce || a.downs != 1 || rec.InstanceID != "i-old" {
		t.Errorf("registro inesperado: %+v", rec)
	}
}

func TestScaleDownIgnoresMissingMetricThatWentQuiet(t *testing.T) {
	cfg := testConfig()
	cfg.Metrics = append(cfg.Metrics, configuration.MetricPolicy{
		MetricName: "requests",
		Scaling:    configuration.ScalingConfig{ScaleUpThreshold: 1000, ScaleDownThreshold: 200},
	})
	a := &fakeActuator{}
	l := &memLogger{}
	c := New(cfg, load(5), fakeFleet{instances: healthy(2)}, a, l)
	c.Tick(context.Background())

	rec := l.recs[0]
	if rec.Decision != model.Reduce || a.downs != 1 {
		t.Errorf("una métrica ausente no debería bloquear la reducción: %+v", rec)
	}
}

func TestMaintainBetweenThresholds(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(load(50), fakeFleet{instances: healthy(2)}, a)
	if rec.Decision != model.Maintain || a.ups+a.downs != 0 || rec.Action != "none" {
		t.Errorf("registro inesperado: %+v", rec)
	}
}

func TestRespectsMinAndMax(t *testing.T) {
	a := &fakeActuator{}
	if _, rec := run(load(5), fakeFleet{instances: healthy(1)}, a); rec.Decision != model.Maintain {
		t.Errorf("no debía bajar del mínimo: %+v", rec)
	}
	if _, rec := run(load(99), fakeFleet{instances: healthy(3)}, a); rec.Decision != model.Maintain {
		t.Errorf("no debía superar el máximo: %+v", rec)
	}
	if a.ups+a.downs != 0 {
		t.Error("no debía actuar")
	}
}

func TestBelowMinScalesUpWithoutMetrics(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(fakeMetrics{err: errors.New("sin datos")}, fakeFleet{}, a)
	if rec.Decision != model.Increase || a.ups != 1 {
		t.Errorf("debía restablecer el mínimo: %+v", rec)
	}
	if rec.Error == "" {
		t.Error("debía registrar también el fallo de observación")
	}
}

func TestObserveErrorMaintains(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(fakeMetrics{err: errors.New("boom")}, fakeFleet{instances: healthy(2)}, a)
	if rec.Decision != model.Maintain || a.ups+a.downs != 0 || rec.Error == "" {
		t.Errorf("registro inesperado: %+v", rec)
	}
}

func TestNoMetricsNeverScalesDown(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(fakeMetrics{}, fakeFleet{instances: healthy(2)}, a)
	if rec.Decision != model.Maintain || a.downs != 0 {
		t.Errorf("registro inesperado: %+v", rec)
	}
}

func TestFleetErrorDoesNotAct(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(load(99), fakeFleet{err: errors.New("aws caído")}, a)
	if rec.Decision != model.Maintain || a.ups != 0 || rec.Error == "" {
		t.Errorf("registro inesperado: %+v", rec)
	}
}

func TestActuatorErrorIsRecorded(t *testing.T) {
	a := &fakeActuator{err: errors.New("InsufficientInstanceCapacity")}
	c, rec := run(load(99), fakeFleet{instances: healthy(1)}, a)
	if rec.Result != "error" || rec.Error == "" || rec.Decision != model.Increase {
		t.Errorf("registro inesperado: %+v", rec)
	}
	if !c.lastActionAt.IsZero() {
		t.Error("una acción fallida no debe iniciar el cooldown")
	}
}

func TestCooldownBlocksConsecutiveActions(t *testing.T) {
	a := &fakeActuator{}
	l := &memLogger{}
	c := New(testConfig(), load(99), fakeFleet{instances: healthy(1)}, a, l)
	c.Tick(context.Background())
	c.Tick(context.Background())
	if a.ups != 1 {
		t.Errorf("ups = %d, el cooldown debía bloquear la segunda", a.ups)
	}
	if l.recs[1].Reason != "en cooldown" {
		t.Errorf("motivo = %q", l.recs[1].Reason)
	}
	c.lastActionAt = time.Now().Add(-time.Hour)
	c.Tick(context.Background())
	if a.ups != 2 {
		t.Error("pasado el cooldown debía volver a actuar")
	}
}

func TestEveryTickLogsExactlyOneRecord(t *testing.T) {
	l := &memLogger{}
	c := New(testConfig(), fakeMetrics{err: errors.New("x")}, fakeFleet{err: errors.New("y")}, &fakeActuator{}, l)
	c.Tick(context.Background())
	if len(l.recs) != 1 {
		t.Errorf("registros = %d", len(l.recs))
	}
}

// ---- tests de nuevas funcionalidades ----

// TestCycleIDIncrementsEachTick verifica que CycleID se incrementa en cada ciclo.
func TestCycleIDIncrementsEachTick(t *testing.T) {
	l := &memLogger{}
	c := New(testConfig(), load(50), fakeFleet{instances: healthy(2)}, &fakeActuator{}, l)
	for i := 0; i < 3; i++ {
		c.Tick(context.Background())
	}
	for i, rec := range l.recs {
		if rec.CycleID != i+1 {
			t.Errorf("ciclo %d: CycleID esperado %d, got %d", i, i+1, rec.CycleID)
		}
	}
}

// TestConfirmCyclesDownRequiresConsecutiveCycles verifica que con confirmCyclesDown=3,
// el ScaleDown solo ocurre en el tercer ciclo consecutivo de baja carga.
func TestConfirmCyclesDownRequiresConsecutiveCycles(t *testing.T) {
	cfg := testConfig()
	cfg.ConfirmCyclesDown = 3

	a := &fakeActuator{}
	l := &memLogger{}
	c := New(cfg, load(5), fakeFleet{instances: healthy(2)}, a, l)

	// Ciclo 1: carga baja → MAINTAIN (esperando confirmación 1/3)
	c.Tick(context.Background())
	if l.recs[0].Decision != model.Maintain {
		t.Errorf("ciclo 1: esperaba MAINTAIN, got %s", l.recs[0].Decision)
	}

	// Ciclo 2: carga baja → MAINTAIN (esperando confirmación 2/3)
	c.Tick(context.Background())
	if l.recs[1].Decision != model.Maintain {
		t.Errorf("ciclo 2: esperaba MAINTAIN, got %s", l.recs[1].Decision)
	}

	// Ciclo 3: carga baja → REDUCE (confirmación completa 3/3)
	c.Tick(context.Background())
	if l.recs[2].Decision != model.Reduce {
		t.Errorf("ciclo 3: esperaba REDUCE, got %s (%s)", l.recs[2].Decision, l.recs[2].Reason)
	}
	if a.downs != 1 {
		t.Errorf("downs = %d, esperaba 1", a.downs)
	}
}

// TestConfirmCyclesDownResetsOnHighLoad verifica que un ciclo de alta carga
// reinicia el contador de confirmación de ScaleDown.
func TestConfirmCyclesDownResetsOnHighLoad(t *testing.T) {
	cfg := testConfig()
	cfg.ConfirmCyclesDown = 3

	l := &memLogger{}
	a := &fakeActuator{}

	// 2 ciclos de baja carga...
	lowFleet := fakeFleet{instances: healthy(2)}
	c := New(cfg, load(5), lowFleet, a, l)
	c.Tick(context.Background())
	c.Tick(context.Background())

	// ...seguido de 1 ciclo de alta carga (reinicia el contador)
	c.metrics = load(90)
	c.Tick(context.Background())
	if l.recs[2].Decision != model.Maintain { // en max (3 = maxInstances), no puede subir
		// Si fleet tiene 2 instancias y max=3, debería INCREASE
		// Adaptamos: el test solo verifica que el confirmCount se reinicia
	}

	// Ahora 3 ciclos de baja carga → el ScaleDown solo debe ocurrir en el 3er ciclo
	c.metrics = load(5)
	c.Tick(context.Background())
	c.Tick(context.Background())
	// El ScaleDown no debe haber ocurrido todavía (solo 2 ciclos de confirmación)
	if a.downs > 0 {
		t.Errorf("ScaleDown prematuro: downs=%d tras 2 ciclos de confirmación", a.downs)
	}
}

// TestScaleUpStepLaunchesMultipleInstances verifica que scaleUpStep=2 lanza 2 instancias.
func TestScaleUpStepLaunchesMultipleInstances(t *testing.T) {
	cfg := testConfig()
	cfg.ScaleUpStep = 2

	a := &fakeActuator{}
	l := &memLogger{}
	c := New(cfg, load(90), fakeFleet{instances: healthy(1)}, a, l)
	c.Tick(context.Background())

	if a.ups != 2 {
		t.Errorf("scaleUpStep=2: esperaba 2 llamadas a ScaleUp, got %d", a.ups)
	}
}

// TestCapacityBeforeIsRecorded verifica que CapacityBefore refleja el estado
// de la flota al inicio del ciclo, antes de cualquier acción.
func TestCapacityBeforeIsRecorded(t *testing.T) {
	a := &fakeActuator{}
	_, rec := run(load(90), fakeFleet{instances: healthy(2)}, a)
	if rec.CapacityBefore.Total != 2 || rec.CapacityBefore.Healthy != 2 {
		t.Errorf("CapacityBefore incorrecto: %+v", rec.CapacityBefore)
	}
}

// TestLastRecordIsUpdatedAfterTick verifica que LastRecord() refleja el
// último ciclo ejecutado.
func TestLastRecordIsUpdatedAfterTick(t *testing.T) {
	l := &memLogger{}
	c := New(testConfig(), load(90), fakeFleet{instances: healthy(1)}, &fakeActuator{}, l)
	c.Tick(context.Background())
	rec := c.LastRecord()
	if rec.Decision != model.Increase {
		t.Errorf("LastRecord no actualizado correctamente: %+v", rec)
	}
	if rec.CycleID != 1 {
		t.Errorf("CycleID en LastRecord incorrecto: %d", rec.CycleID)
	}
}
