"""
FISCO-BCOS 轻节点配置文件生成器

在容器启动时根据环境变量动态生成:
  - config.ini       轻节点主配置 (连接全节点的 P2P 地址)
  - config.genesis    创世块配置 (与全节点一致, 由全节点网络提供挂载)

FISCO-BCOS v3.x light node 架构:
  light_node/
  ├── fisco-bcos          # 轻节点二进制 (或 symlink)
  ├── conf/
  │   ├── config.ini      # 主配置 — 指定连接的全节点 P2P endpoints
  │   ├── config.genesis  # 创世块 — 需与全节点网络一致
  │   ├── node.key        # 节点私钥
  │   ├── node.crt        # 节点证书
  │   ├── ca.crt          # CA 证书
  │   └── ssl.key / ssl.crt / sm_ssl.*  # SSL 通信证书
  └── data/               # 运行时数据
"""
import os
import logging
import secrets

logger = logging.getLogger(__name__)

LIGHT_NODE_DIR = os.environ.get("FISCO_LIGHT_NODE_DIR", "/fisco/light_node")
CONF_DIR = os.path.join(LIGHT_NODE_DIR, "conf")


def generate_config_ini(full_node_endpoints: list, group_id: int = 1,
                        rpc_listen_port: int = 20200, node_id: int = 0) -> str:
    """
    生成 FISCO-BCOS v3.x 轻节点 config.ini

    轻节点通过 [p2p] 配置连接到全节点网络, 通过 [chain] 指定链 ID 与 group,
    通过 [rpc] 暴露本地 JSON-RPC 供应用层使用。
    """

    # 构建全节点 peers 列表
    peers_lines = ""
    for i, ep in enumerate(full_node_endpoints):
        peers_lines += f'    node.{i}={ep}\n'

    config_ini = f"""\
[rpc]
    listen_ip=0.0.0.0
    listen_port=20200
    thread_count=4
    ; 轻节点本地 JSON-RPC, 供 Python 应用层调用

[p2p]
    listen_ip=0.0.0.0
    listen_port=30300
    ; sm_ssl=false
    ; 全节点连接地址
    nodes_path=./conf
    nodes_file=nodes.json

[chain]
    ; 链 ID, 必须与全节点一致
    chain_id=chain0
    ; group ID
    group_id=group{group_id}
    ; 轻节点模式
    sm_crypto=false

[storage]
    data_path=./data
    enable_cache=true

[log]
    enable=true
    log_path=./log
    ; log level: TRACE, DEBUG, INFO, WARNING, ERROR, FATAL
    level=INFO
    max_log_file_size=200

[security]
    key=conf/node.key
    cert=conf/node.crt
    ca_cert=conf/ca.crt
"""
    return config_ini


def generate_nodes_json(full_node_endpoints: list) -> str:
    """
    生成 nodes.json — 轻节点需要连接的全节点 P2P 地址列表
    FISCO v3.x 使用 nodes.json 来指定对端节点
    """
    import json
    nodes = []
    for ep in full_node_endpoints:
        host, port = ep.rsplit(":", 1)
        nodes.append({
            "host": host,
            "port": int(port)
        })
    return json.dumps({"nodes": nodes}, indent=2)


def generate_node_key() -> str:
    """生成一个简单的节点私钥（EC secp256k1 hex 格式）"""
    return secrets.token_hex(32)


def generate_config_genesis(group_id: int = 1) -> str:
    """
    生成基础的 config.genesis 文件
    注意: 正式部署时，config.genesis 应与全节点一致（通常从全节点拷贝）
    此处生成一个默认模板，支持通过挂载覆盖
    """
    config_genesis = f"""\
[chain]
    chain_id=chain0
    sm_crypto=false
    group_id=group{group_id}

[consensus]
    consensus_type=pbft
    block_tx_count_limit=1000
    leader_period=1

[tx]
    gas_limit=300000000

[executor]
    is_wasm=false

[version]
    compatibility_version={group_id}.0.0
"""
    return config_genesis


def write_light_node_config(full_node_endpoints: list, group_id: int = 1,
                            node_id: int = 0):
    """
    将所有配置文件写入轻节点目录

    如果 /fisco/light_node/conf/ 下已存在外部挂载的证书/配置，保留不覆盖。
    """
    os.makedirs(CONF_DIR, exist_ok=True)
    os.makedirs(os.path.join(LIGHT_NODE_DIR, "data"), exist_ok=True)
    os.makedirs(os.path.join(LIGHT_NODE_DIR, "log"), exist_ok=True)

    # 1. config.ini — 始终根据环境变量重新生成
    config_ini_path = os.path.join(CONF_DIR, "config.ini")
    config_ini = generate_config_ini(full_node_endpoints, group_id, node_id=node_id)
    with open(config_ini_path, "w") as f:
        f.write(config_ini)
    logger.info(f"generated {config_ini_path}")

    # 2. nodes.json — 全节点地址列表
    nodes_json_path = os.path.join(CONF_DIR, "nodes.json")
    nodes_json = generate_nodes_json(full_node_endpoints)
    with open(nodes_json_path, "w") as f:
        f.write(nodes_json)
    logger.info(f"generated {nodes_json_path} with {len(full_node_endpoints)} full node(s)")

    # 3. config.genesis — 仅当不存在时生成默认版本
    genesis_path = os.path.join(CONF_DIR, "config.genesis")
    if not os.path.exists(genesis_path):
        genesis = generate_config_genesis(group_id)
        with open(genesis_path, "w") as f:
            f.write(genesis)
        logger.info(f"generated default {genesis_path} (should be overridden by full node's genesis)")
    else:
        logger.info(f"using existing {genesis_path} (mounted from full node)")

    # 4. node.key — 仅当不存在时生成
    key_path = os.path.join(CONF_DIR, "node.key")
    if not os.path.exists(key_path):
        key_hex = generate_node_key()
        with open(key_path, "w") as f:
            f.write(key_hex)
        logger.info(f"generated new node key at {key_path}")
    else:
        logger.info(f"using existing node key at {key_path}")

    # 5. 检查证书文件
    for cert_file in ["ca.crt", "node.crt", "ssl.key", "ssl.crt"]:
        cert_path = os.path.join(CONF_DIR, cert_file)
        if not os.path.exists(cert_path):
            logger.warning(f"certificate not found: {cert_path} — "
                           f"mount from full node or run certificate generation tool")

    # 6. 创建 fisco-bcos symlink
    fisco_binary = os.environ.get("FISCO_BINARY", "/fisco/fisco-bcos")
    local_binary = os.path.join(LIGHT_NODE_DIR, "fisco-bcos")
    if os.path.exists(fisco_binary) and not os.path.exists(local_binary):
        os.symlink(fisco_binary, local_binary)
        logger.info(f"symlinked {fisco_binary} -> {local_binary}")

    return config_ini_path
