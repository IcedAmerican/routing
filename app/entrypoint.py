#!/usr/bin/env python3
"""
routing 容器入口脚本

启动顺序:
  1. 配置 FRR 路由 (利用 satellite_emulator 生成的配置)
  2. 根据环境变量动态生成 FISCO-BCOS 轻节点配置
  3. 启动 fisco-bcos 轻节点守护进程
  4. 等待轻节点就绪 (健康检查)
  5. 启动 RPC 服务 (Flask)
"""
import os
import signal
import subprocess
import sys
import time
import logging
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s"
)
logger = logging.getLogger("entrypoint")

# 轻节点进程全局引用, 用于优雅退出
_fisco_process = None


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


def generate_light_node_config():
    """根据环境变量生成轻节点配置文件"""
    from app.config_generator import write_light_node_config

    full_nodes_str = os.environ.get("FISCO_FULL_NODES", "")
    if not full_nodes_str:
        logger.warning("FISCO_FULL_NODES not set, light node may not connect to full nodes")
        return

    full_node_endpoints = [ep.strip() for ep in full_nodes_str.split(",") if ep.strip()]
    group_id = int(os.environ.get("FISCO_GROUP_ID", "1"))
    node_id = int(os.environ.get("NODE_ID", "0"))

    logger.info(f"generating light node config for node {node_id}")
    logger.info(f"full node endpoints: {full_node_endpoints}")

    write_light_node_config(
        full_node_endpoints=full_node_endpoints,
        group_id=group_id,
        node_id=node_id
    )


def start_fisco_light_node() -> subprocess.Popen:
    """
    启动 FISCO-BCOS 轻节点守护进程

    fisco-bcos 以轻节点模式运行, 参数:
      -c config.ini 路径
      -g config.genesis 路径

    轻节点启动后:
      - 通过 P2P 连接到全节点网络
      - 同步区块头和交易回执 (不存储完整区块数据)
      - 在本地 20200 端口暴露 JSON-RPC 供应用调用
    """
    global _fisco_process

    light_node_dir = os.environ.get("FISCO_LIGHT_NODE_DIR", "/fisco/light_node")
    fisco_binary = os.path.join(light_node_dir, "fisco-bcos")

    if not os.path.exists(fisco_binary):
        # 尝试使用全局路径
        fisco_binary = os.environ.get("FISCO_BINARY", "/fisco/fisco-bcos")

    if not os.path.exists(fisco_binary):
        logger.error(f"fisco-bcos binary not found at {fisco_binary}")
        logger.error("light node will NOT start — RPC server will run in mock mode")
        return None

    config_ini = os.path.join(light_node_dir, "conf", "config.ini")
    config_genesis = os.path.join(light_node_dir, "conf", "config.genesis")

    if not os.path.exists(config_ini):
        logger.error(f"config.ini not found at {config_ini}")
        return None

    # 构建启动命令
    cmd = [
        fisco_binary,
        "-c", config_ini,
        "-g", config_genesis,
    ]

    logger.info(f"starting FISCO light node: {' '.join(cmd)}")
    logger.info(f"working directory: {light_node_dir}")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=light_node_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # 轻节点以后台守护进程方式运行
        )
        _fisco_process = proc
        logger.info(f"FISCO light node started, PID={proc.pid}")

        # 异步输出轻节点日志 (非阻塞)
        import threading

        def _log_output(proc):
            try:
                for line in iter(proc.stdout.readline, b''):
                    logger.info(f"[fisco-bcos] {line.decode('utf-8', errors='replace').rstrip()}")
            except Exception:
                pass

        log_thread = threading.Thread(target=_log_output, args=(proc,), daemon=True)
        log_thread.start()

        return proc
    except Exception as e:
        logger.error(f"failed to start FISCO light node: {e}")
        return None


def wait_for_light_node_ready(timeout: int = 60) -> bool:
    """
    等待轻节点就绪: 检测本地 JSON-RPC 端口可用

    轻节点就绪标志: 本地 20200 端口的 JSON-RPC 能响应 getClientVersion
    """
    import requests

    rpc_url = "http://127.0.0.1:20200"
    start = time.time()

    logger.info(f"waiting for light node RPC at {rpc_url} (timeout={timeout}s)...")

    while time.time() - start < timeout:
        try:
            resp = requests.post(
                rpc_url,
                json={"jsonrpc": "2.0", "method": "getClientVersion", "params": [], "id": 1},
                timeout=3
            )
            if resp.status_code == 200:
                data = resp.json()
                if "result" in data or "FISCO" in resp.text:
                    logger.info(f"light node ready! response: {resp.text[:200]}")
                    return True
        except requests.exceptions.ConnectionError:
            pass
        except Exception as e:
            logger.debug(f"health check attempt failed: {e}")

        time.sleep(2)

    logger.warning(f"light node did not become ready within {timeout}s, "
                   f"proceeding with potentially degraded mode")
    return False


def check_fisco_binary():
    """检查 fisco-bcos 二进制是否存在且可执行"""
    candidates = [
        os.environ.get("FISCO_BINARY", "/fisco/fisco-bcos"),
        "/fisco/light_node/fisco-bcos",
        "/usr/local/bin/fisco-bcos",
    ]
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info(f"found fisco-bcos binary: {path}")
            # 打印版本
            try:
                result = subprocess.run([path, "-v"], capture_output=True, text=True, timeout=5)
                logger.info(f"fisco-bcos version: {result.stdout.strip()}")
            except Exception:
                pass
            return path
    logger.warning("fisco-bcos binary NOT found in any expected location")
    return None


def graceful_shutdown(signum, frame):
    """优雅退出: 先停止轻节点再退出"""
    logger.info(f"received signal {signum}, shutting down...")
    global _fisco_process
    if _fisco_process and _fisco_process.poll() is None:
        logger.info(f"terminating FISCO light node (PID={_fisco_process.pid})...")
        _fisco_process.terminate()
        try:
            _fisco_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _fisco_process.kill()
        logger.info("FISCO light node stopped")
    sys.exit(0)


def main():
    node_id = os.environ.get("NODE_ID", "?")
    logger.info(f"======== routing node {node_id} starting ========")
    logger.info(f"FISCO_FULL_NODES={os.environ.get('FISCO_FULL_NODES', 'not set')}")
    logger.info(f"BLACKLIST_CONTRACT={os.environ.get('BLACKLIST_CONTRACT', 'not set')}")
    logger.info(f"FISCO_GROUP_ID={os.environ.get('FISCO_GROUP_ID', 'not set')}")

    # 注册信号处理
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    # 1. 配置网络路由
    setup_frr()
    apply_routes()

    # 2. 检查 fisco-bcos 二进制
    fisco_binary = check_fisco_binary()

    # 3. 生成轻节点配置
    generate_light_node_config()

    # 4. 启动 FISCO 轻节点进程
    fisco_proc = None
    if fisco_binary:
        fisco_proc = start_fisco_light_node()
        if fisco_proc:
            # 5. 等待轻节点就绪
            ready = wait_for_light_node_ready(timeout=60)
            if not ready:
                logger.warning("light node not fully ready, RPC server will handle errors gracefully")
    else:
        logger.warning("no fisco-bcos binary available, running in MOCK mode")
        logger.warning("all blockchain operations will be simulated locally")

    # 6. 启动 RPC 服务 (阻塞)
    logger.info("starting RPC server...")
    from app.rpc_server import main as rpc_main
    rpc_main()


if __name__ == "__main__":
    main()
