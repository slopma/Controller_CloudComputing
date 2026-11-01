package configuration

import (
	"encoding/json"
	"fmt"
	"os"
)

type AWSConfig struct {
	Region string
}

type ComputeConfig struct {
	LaunchTemplateID      string
	LaunchTemplateVersion string
	MinInstances          int
	MaxInstances          int
	SubnetIDs             []string
	SecurityGroupIDs      []string
}

type LoadBalancerConfig struct {
	TargetGroupARN string
	TargetPort     int
}

type ScalingConfig struct {
	ScaleUpThreshold   float64
	ScaleDownThreshold float64
}

type Dimension struct {
	Name  string
	Value string
}

type MetricPolicy struct {
	Namespace  string
	MetricName string
	Dimensions []Dimension
	// PerInstance marca una métrica que no existe a nivel de target group (p. ej.
	// CPUUtilization): el observer la consulta por cada instancia de la flota
	// (dimensión InstanceId) y promedia los resultados. No debe traer Dimensions.
	PerInstance bool
	Period      int
	Stat        string
	Scaling     ScalingConfig
}

type DeploymentConfig struct {
	UseDocker bool
}

type Config struct {
	AWS          AWSConfig
	Compute      ComputeConfig
	LoadBalancer LoadBalancerConfig

	PollIntervalSeconds      int
	ObservationWindowSeconds int
	CooldownSeconds          int

	// ScaleUpStep es cuántas instancias se lanzan por decisión INCREASE_CAPACITY.
	// Permite escalar más agresivamente cuando la carga supera el umbral con margen.
	// Valor por defecto: 1 (conservador).
	ScaleUpStep int

	// ScaleDownStep es cuántas instancias se retiran por decisión REDUCE_CAPACITY.
	// Valor por defecto: 1.
	ScaleDownStep int

	// ConfirmCyclesDown es el número de ciclos CONSECUTIVOS en que TODAS las
	// métricas deben estar bajo su umbral de bajada antes de ejecutar un ScaleDown.
	// Proporciona histéresis temporal: evita reaccionar a caídas transitorias de carga.
	// Ej: con pollInterval=60s y confirmCyclesDown=3, se espera 3 min de carga baja.
	// Valor por defecto: 1 (comportamiento equivalente a la versión original).
	ConfirmCyclesDown int

	// StatusPort es el puerto TCP donde el controller expone los endpoints HTTP:
	//   GET /health → estado del proceso (siempre 200 si está vivo)
	//   GET /status → último DecisionRecord serializado como JSON
	// 0 deshabilita el servidor HTTP de estado (modo silencioso).
	StatusPort int

	// StateFile es la ruta del archivo JSON donde se persiste lastActionAt entre
	// reinicios. Si el proceso se reinicia, el cooldown se reanuda correctamente
	// y no se actúa precipitadamente en el primer tick.
	// Vacío ("") deshabilita la persistencia (comportamiento original).
	StateFile string

	Metrics    []MetricPolicy
	Deployment DeploymentConfig
}

// Defaults aplica valores por defecto a campos opcionales con valor cero.
// Se llama automáticamente en Load() para garantizar compatibilidad con
// archivos de configuración existentes que no declaran los nuevos campos.
func (c *Config) Defaults() {
	if c.ScaleUpStep <= 0 {
		c.ScaleUpStep = 1
	}
	if c.ScaleDownStep <= 0 {
		c.ScaleDownStep = 1
	}
	if c.ConfirmCyclesDown <= 0 {
		c.ConfirmCyclesDown = 1
	}
}

func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("leyendo config: %w", err)
	}

	var config Config
	if err := json.Unmarshal(data, &config); err != nil {
		return nil, fmt.Errorf("parseando config: %w", err)
	}

	config.Defaults()

	if err := config.Validate(); err != nil {
		return nil, fmt.Errorf("config inválida: %w", err)
	}

	return &config, nil
}

func (c *Config) Validate() error {
	if c.AWS.Region == "" {
		return fmt.Errorf("aws.region es obligatorio")
	}
	if c.Compute.LaunchTemplateID == "" {
		return fmt.Errorf("compute.launchTemplateId es obligatorio")
	}
	if c.LoadBalancer.TargetGroupARN == "" {
		return fmt.Errorf("loadBalancer.targetGroupArn es obligatorio")
	}
	if len(c.Compute.SubnetIDs) == 0 {
		return fmt.Errorf("compute.subnetIds necesita al menos una subred")
	}
	if c.Compute.MinInstances < 1 {
		return fmt.Errorf("minInstances debe ser al menos 1")
	}
	if c.Compute.MaxInstances > 5 {
		return fmt.Errorf("maxInstances debe ser como máximo 5")
	}
	if c.Compute.MinInstances > c.Compute.MaxInstances {
		return fmt.Errorf("minInstances debe ser menor o igual a maxInstances")
	}
	if c.PollIntervalSeconds <= 0 {
		return fmt.Errorf("pollIntervalSeconds debe ser mayor que 0")
	}
	if c.CooldownSeconds < 0 {
		return fmt.Errorf("cooldownSeconds no puede ser negativo")
	}
	if c.ScaleUpStep < 1 || c.ScaleUpStep > 2 {
		return fmt.Errorf("scaleUpStep debe ser 1 o 2")
	}
	if c.ScaleDownStep < 1 || c.ScaleDownStep > 2 {
		return fmt.Errorf("scaleDownStep debe ser 1 o 2")
	}
	if c.ConfirmCyclesDown < 1 {
		return fmt.Errorf("confirmCyclesDown debe ser al menos 1")
	}
	if c.StatusPort < 0 || c.StatusPort > 65535 {
		return fmt.Errorf("statusPort debe estar entre 0 y 65535")
	}
	if len(c.Metrics) == 0 {
		return fmt.Errorf("se necesita al menos 1 política de métrica")
	}

	for _, m := range c.Metrics {
		if m.Namespace == "" || m.MetricName == "" || m.Stat == "" {
			return fmt.Errorf("métrica %q: namespace, metricName y stat son obligatorios", m.MetricName)
		}
		if m.PerInstance && len(m.Dimensions) > 0 {
			return fmt.Errorf("métrica %q: perInstance no debe traer dimensions, se generan una por instancia", m.MetricName)
		}
		if m.Period <= 0 || m.Period%60 != 0 {
			return fmt.Errorf("métrica %q: period debe ser un múltiplo positivo de 60", m.MetricName)
		}
		if c.ObservationWindowSeconds < m.Period {
			return fmt.Errorf("observationWindowSeconds debe ser al menos el period de %q", m.MetricName)
		}
		if m.Scaling.ScaleUpThreshold <= m.Scaling.ScaleDownThreshold {
			return fmt.Errorf("métrica %q: scaleUpThreshold debe ser mayor que scaleDownThreshold", m.MetricName)
		}
	}

	return nil
}
