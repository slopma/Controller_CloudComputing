# punto de entrada principal para correr el auto-scaling controller

import argparse
import logging
import os
import signal
import sys
import threading
from datetime import timedelta
from pathlib import Path

import boto3

from controllerasg.configuration import load_config
from controllerasg.controller import Controller
from controllerasg.logger import JSONLogger
from controllerasg.observer import CloudWatchObserver
from controllerasg.provisioner import Provisioner
from controllerasg.statusserver import StatusServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("controllerasg")


# parseo de flags de linea de comandos
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ControllerASG: Auto-Scaling Controller para AWS"
    )
    parser.add_argument(
        "-config",
        "--config",
        dest="config",
        default="conf.json",
        help="ruta del archivo conf.json (default: conf.json)",
    )
    parser.add_argument(
        "-log",
        "--log",
        dest="log",
        default="decisions.jsonl",
        help="archivo jsonl donde se guardan las decisiones",
    )
    parser.add_argument(
        "-check",
        "--check",
        dest="check",
        action="store_true",
        help="solo valida la configuracion y sale sin llamar a aws",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # cargamos la configuracion
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.error(f"No se pudo cargar la configuración: {e}")
        sys.exit(1)

    # si solo era para validar (-check) salimos de una
    if args.check:
        logger.info(f"configuración válida: {args.config}")
        logger.info(
            f"  región={config.aws.region}, min={config.compute.min_instances}, "
            f"max={config.compute.max_instances}, ciclo={config.poll_interval_seconds}s, "
            f"cooldown={config.cooldown_seconds}s, confirmCycles={config.confirm_cycles_down}, "
            f"scaleUpStep={config.scale_up_step}, scaleDownStep={config.scale_down_step}"
        )
        return

    # abrimos el archivo de log para append
    try:
        log_file = open(args.log, "a", encoding="utf-8")
    except Exception as e:
        logger.error(f"No se pudo abrir el log de decisiones: {e}")
        sys.exit(1)

    stop_event = threading.Event()

    # para capturar ctrl+c y apagar bien
    def handle_signal(sig: int, frame: object) -> None:
        logger.info("señal recibida, deteniendo el controller...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # conectamos con los clientes de aws
    try:
        session = boto3.Session(region_name=config.aws.region)
        cw_client = session.client("cloudwatch")
        ec2_client = session.client("ec2")
        elb_client = session.client("elbv2")
    except Exception as e:
        logger.error(f"No se pudo inicializar la sesión de AWS: {e}")
        sys.exit(1)

    window = timedelta(seconds=config.observation_window_seconds)
    obs = CloudWatchObserver(cw_client, config.metrics, window)
    prov = Provisioner(ec2_client, elb_client, config)
    json_logger = JSONLogger(sys.stdout, log_file)
    ctl = Controller(config, obs, prov, prov, json_logger)

    status_server: StatusServer | None = None
    if config.status_port > 0:
        status_server = StatusServer(config.status_port, ctl)
        status_server.start()
        logger.info(f"status HTTP en http://0.0.0.0:{config.status_port}/status")

    logger.info(
        f"controller iniciado — ciclo={config.poll_interval_seconds}s "
        f"mín={config.compute.min_instances} máx={config.compute.max_instances} "
        f"cooldown={config.cooldown_seconds}s confirmCycles={config.confirm_cycles_down} "
        f"scaleStep={config.scale_up_step}↑/{config.scale_down_step}↓ log={args.log}"
    )
    if config.state_file:
        logger.info(f"persistencia de cooldown en {config.state_file}")

    # primer tick inmediato sin esperar al timer
    ctl.tick()

    # bucle que corre cada poll_interval_seconds
    while not stop_event.is_set():
        if stop_event.wait(timeout=float(config.poll_interval_seconds)):
            break
        ctl.tick()

    if status_server is not None:
        status_server.stop()

    log_file.close()
    logger.info("controller detenido.")


if __name__ == "__main__":
    main()
