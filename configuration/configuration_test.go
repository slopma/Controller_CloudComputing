package configuration

import (
	"os"
	"path/filepath"
	"testing"
)

func validJSON() string {
	return `{
		"aws": { "region": "us-east-1" },
		"compute": {
			"launchTemplateId": "lt-12345",
			"minInstances": 1,
			"maxInstances": 5,
			"subnetIds": ["subnet-1"]
		},
		"loadBalancer": {
			"targetGroupArn": "arn:aws:tg-1",
			"targetPort": 80
		},
		"pollIntervalSeconds": 60,
		"observationWindowSeconds": 300,
		"cooldownSeconds": 300,
		"metrics": [
			{
				"namespace": "AWS/EC2",
				"metricName": "CPUUtilization",
				"stat": "Average",
				"period": 60,
				"scaling": { "scaleUpThreshold": 70, "scaleDownThreshold": 20 }
			}
		]
	}`
}

func TestLoadDefaults(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "conf.json")
	if err := os.WriteFile(path, []byte(validJSON()), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load falló: %v", err)
	}

	if cfg.ScaleUpStep != 1 {
		t.Errorf("ScaleUpStep por defecto esperado 1, got %d", cfg.ScaleUpStep)
	}
	if cfg.ScaleDownStep != 1 {
		t.Errorf("ScaleDownStep por defecto esperado 1, got %d", cfg.ScaleDownStep)
	}
	if cfg.ConfirmCyclesDown != 1 {
		t.Errorf("ConfirmCyclesDown por defecto esperado 1, got %d", cfg.ConfirmCyclesDown)
	}
	if cfg.StatusPort != 0 {
		t.Errorf("StatusPort por defecto esperado 0, got %d", cfg.StatusPort)
	}
}

func TestLoadCustomFields(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "conf.json")
	custom := `{
		"aws": { "region": "us-east-1" },
		"compute": {
			"launchTemplateId": "lt-12345",
			"minInstances": 1,
			"maxInstances": 5,
			"subnetIds": ["subnet-1"]
		},
		"loadBalancer": {
			"targetGroupArn": "arn:aws:tg-1",
			"targetPort": 80
		},
		"pollIntervalSeconds": 30,
		"observationWindowSeconds": 120,
		"cooldownSeconds": 180,
		"scaleUpStep": 2,
		"scaleDownStep": 1,
		"confirmCyclesDown": 3,
		"statusPort": 9090,
		"stateFile": "state.json",
		"metrics": [
			{
				"namespace": "AWS/EC2",
				"metricName": "CPUUtilization",
				"stat": "Average",
				"period": 60,
				"scaling": { "scaleUpThreshold": 75, "scaleDownThreshold": 25 }
			}
		]
	}`
	if err := os.WriteFile(path, []byte(custom), 0o644); err != nil {
		t.Fatal(err)
	}

	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("Load falló: %v", err)
	}

	if cfg.ScaleUpStep != 2 {
		t.Errorf("ScaleUpStep esperado 2, got %d", cfg.ScaleUpStep)
	}
	if cfg.ConfirmCyclesDown != 3 {
		t.Errorf("ConfirmCyclesDown esperado 3, got %d", cfg.ConfirmCyclesDown)
	}
	if cfg.StatusPort != 9090 {
		t.Errorf("StatusPort esperado 9090, got %d", cfg.StatusPort)
	}
	if cfg.StateFile != "state.json" {
		t.Errorf("StateFile esperado 'state.json', got %s", cfg.StateFile)
	}
}

func TestValidationRejectsInvalid(t *testing.T) {
	cases := []struct {
		name    string
		mutate  func(*Config)
		wantErr bool
	}{
		{"min > max", func(c *Config) { c.Compute.MinInstances = 5; c.Compute.MaxInstances = 2 }, true},
		{"scaleUpStep < 1", func(c *Config) { c.ScaleUpStep = 0 }, true},
		{"confirmCyclesDown < 1", func(c *Config) { c.ConfirmCyclesDown = 0 }, true},
		{"statusPort out of range", func(c *Config) { c.StatusPort = 70000 }, true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "conf.json")
			if err := os.WriteFile(path, []byte(validJSON()), 0o644); err != nil {
				t.Fatal(err)
			}
			cfg, err := Load(path)
			if err != nil {
				t.Fatal(err)
			}
			tc.mutate(cfg)
			valErr := cfg.Validate()
			if (valErr != nil) != tc.wantErr {
				t.Errorf("Validate() error = %v, wantErr %v", valErr, tc.wantErr)
			}
		})
	}
}
