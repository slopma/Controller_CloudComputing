"""Prueba de integración real contra AWS EC2 y ELBv2.

Ejecutar con:
    CONFIG_PATH=conf.json pytest -m integration -s tests/test_provisioner_integration.py
"""

import os
import time
import pytest
import boto3

from controllerasg.configuration import load_config
from controllerasg.provisioner import Provisioner


@pytest.mark.integration
def test_integration_scale_up_and_down():
    path = os.getenv("CONFIG_PATH", "conf.json")
    if not os.path.exists(path):
        pytest.skip(f"sin archivo de configuración {path}")

    try:
        cfg = load_config(path)
    except Exception as e:
        pytest.skip(f"sin config válida ({e})")

    cfg.compute.min_instances = 0  # parte de flota vacía

    session = boto3.Session(region_name=cfg.aws.region)
    ec2 = session.client("ec2")
    elbv2 = session.client("elbv2")

    p = Provisioner(ec2, elbv2, cfg)

    before = p.instances()
    assert len(before) == 0, f"se esperaba flota vacía, hay {len(before)} instancias gestionadas"

    launched_id = None
    try:
        start = time.time()
        launched_id = p.scale_up()
        print(f"ScaleUp completado en {time.time() - start:.1f}s ({launched_id} en running y registrada)")

        # Esperar a que esté healthy en el target group
        healthy = False
        deadline = time.time() + 300
        while time.time() < deadline:
            fleet = p.instances()
            if fleet and fleet[0].health == "healthy":
                healthy = True
                break
            time.sleep(5)

        assert healthy, "la instancia no llegó a healthy a tiempo"

        # ScaleDown
        victim_id = p.scale_down()
        assert victim_id == launched_id
        print("ScaleDown completado exitosamente")

    finally:
        # Limpieza de seguridad
        if launched_id:
            try:
                ec2.terminate_instances(InstanceIds=[launched_id])
            except Exception:
                pass
