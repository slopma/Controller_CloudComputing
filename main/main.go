package main

import (
	"context"
	"flag"
	"io"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"

	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/cloudwatch"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	elbv2 "github.com/aws/aws-sdk-go-v2/service/elasticloadbalancingv2"

	"controllerasg/configuration"
	"controllerasg/controller"
	"controllerasg/logger"
	"controllerasg/observer"
	"controllerasg/provisioner"
	"controllerasg/statusserver"
)

func main() {
	configPath := flag.String("config", "conf.json", "ruta del archivo de configuración")
	logPath := flag.String("log", "decisions.jsonl", "archivo donde se anota cada decisión (JSON Lines)")
	checkOnly := flag.Bool("check", false, "solo carga y valida la configuración, no contacta AWS ni inicia el ciclo")
	flag.Parse()

	config, err := configuration.Load(*configPath)
	if err != nil {
		log.Fatalf("No se pudo cargar la configuración: %v", err)
	}
	if *checkOnly {
		log.Printf("configuración válida: %s", *configPath)
		log.Printf("  región=%s, min=%d, max=%d, ciclo=%ds, cooldown=%ds, confirmCycles=%d, scaleUpStep=%d, scaleDownStep=%d",
			config.AWS.Region,
			config.Compute.MinInstances,
			config.Compute.MaxInstances,
			config.PollIntervalSeconds,
			config.CooldownSeconds,
			config.ConfirmCyclesDown,
			config.ScaleUpStep,
			config.ScaleDownStep,
		)
		return
	}

	logFile, err := os.OpenFile(*logPath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		log.Fatalf("No se pudo abrir el log de decisiones: %v", err)
	}
	defer logFile.Close()

	// SIGINT/SIGTERM (systemd, Ctrl+C) cancelan el contexto; el ciclo termina limpio.
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	awsCfg, err := awsconfig.LoadDefaultConfig(ctx, awsconfig.WithRegion(config.AWS.Region))
	if err != nil {
		log.Fatalf("No se pudo cargar la configuración de AWS: %v", err)
	}

	obs := observer.New(
		cloudwatch.NewFromConfig(awsCfg),
		config.Metrics,
		time.Duration(config.ObservationWindowSeconds)*time.Second,
	)
	prov := provisioner.New(ec2.NewFromConfig(awsCfg), elbv2.NewFromConfig(awsCfg), config)
	ctl := controller.New(config, obs, prov, prov, logger.New(io.MultiWriter(os.Stdout, logFile)))

	// Servidor HTTP de estado (si statusPort > 0 en config).
	go statusserver.Start(ctx, config.StatusPort, ctl)

	log.Printf("controller iniciado — ciclo=%ds mín=%d máx=%d cooldown=%ds confirmCycles=%d scaleStep=%d↑/%d↓ log=%s",
		config.PollIntervalSeconds,
		config.Compute.MinInstances,
		config.Compute.MaxInstances,
		config.CooldownSeconds,
		config.ConfirmCyclesDown,
		config.ScaleUpStep,
		config.ScaleDownStep,
		*logPath,
	)
	if config.StatusPort > 0 {
		log.Printf("status HTTP en http://localhost:%d/status", config.StatusPort)
	}
	if config.StateFile != "" {
		log.Printf("persistencia de cooldown en %s", config.StateFile)
	}

	ticker := time.NewTicker(time.Duration(config.PollIntervalSeconds) * time.Second)
	defer ticker.Stop()

	// Ejecutar el primer tick inmediatamente sin esperar el primer tick del timer.
	ctl.Tick(ctx)

	for {
		select {
		case <-ctx.Done():
			log.Println("señal recibida, controller detenido")
			return
		case <-ticker.C:
			ctl.Tick(ctx)
		}
	}
}
