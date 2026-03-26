#!/usr/bin/env python3
"""
routing 容器入口脚本
1. 启动 FRR 路由守护进程 (利用 satellite_emulator 生成的配置)
2. 应用路由表
3. 启动 RPC 服务 (Flask)
"""
import os
import subprocess
import sys
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)
logger = logging.getLogger("entrypoint")


def setup_frr():
    """启动 FRR 并加载卫星网络生成的路由配置"""
    node_type = os.environ.get("NODE_TYPE", "routing_node")
    node_id = os.environ.get("NODE_ID", "0")
    frr_conf = f"/configuration/frr/{node_type}_{node_id}.conf"

    if os.path.exists(frr_conf):
        logger.info(f"loading FRR config from {frr_conf}")
        try:
            subprocess.run(["cp", frr_conf, "/etc/frr/frr.conf"], check=True)
            subprocess.run(["service", "frr", "restart"], check=True)
            logger.info("FRR started successfully")
        except subprocess.CalledProcessError as e:
            logger.warning(f"FRR setup failed: {e}")
    else:
        logger.warning(f"FRR config not found: {frr_conf}, skipping FRR setup")


def apply_routes():
    """应用预计算的路由表"""
    node_type = os.environ.get("NODE_TYPE", "routing_node")
    node_id = os.environ.get("NODE_ID", "0")
    routes_conf = f"/configuration/routes/{node_type}_{node_id}.conf"

    if os.path.exists(routes_conf):
        logger.info(f"applying routes from {routes_conf}")
        try:
            with open(routes_conf, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        subprocess.run(line, shell=True, check=False)
            logger.info("routes applied successfully")
        except Exception as e:
            logger.warning(f"route application failed: {e}")
    else:
        logger.warning(f"routes config not found: {routes_conf}")


def main():
    logger.info(f"routing node starting: NODE_ID={os.environ.get('NODE_ID', '?')}")
    logger.info(f"FISCO full nodes: {os.environ.get('FISCO_FULL_NODES', 'none')}")
    logger.info(f"blacklist contract: {os.environ.get('BLACKLIST_CONTRACT', 'none')}")

    # 1. 配置网络路由
    setup_frr()
    apply_routes()

    # 2. 短暂等待网络就绪
    time.sleep(2)

    # 3. 启动 RPC 服务
    logger.info("starting RPC server...")
    from app.rpc_server import main as rpc_main
    rpc_main()


if __name__ == "__main__":
    main()
