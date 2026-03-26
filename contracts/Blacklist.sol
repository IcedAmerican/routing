// SPDX-License-Identifier: MIT
pragma solidity ^0.6.10;
pragma experimental ABIEncoderV2;

/// @title Blacklist - 卫星节点黑名单合约
/// @notice 用于 LEO 卫星网络中检测到恶意行为后提交指控并查询节点状态
contract Blacklist {
    // 节点状态
    enum NodeStatus { ACTIVE, BLOCKED }

    // 指控记录
    struct Accusation {
        address accuser;      // 指控发起方 (轻节点地址)
        string  nodeId;       // 被指控节点 ID
        string  reason;       // 指控原因
        uint256 timestamp;    // 指控时间戳
    }

    // 被指控阈值: 同一节点被不同指控方指控达到此数量即被拉黑
    uint256 public threshold;
    address public admin;

    // nodeId => 当前状态
    mapping(string => NodeStatus) public nodeStatus;
    // nodeId => 指控列表
    mapping(string => Accusation[]) public accusations;
    // nodeId => (accuser => 是否已指控), 防止重复指控
    mapping(string => mapping(address => bool)) public hasAccused;
    // nodeId => 独立指控方计数
    mapping(string => uint256) public accuserCount;

    event NodeAccused(string indexed nodeId, address indexed accuser, string reason, uint256 timestamp);
    event NodeBlocked(string indexed nodeId, uint256 accuserCount, uint256 timestamp);
    event NodeUnblocked(string indexed nodeId, uint256 timestamp);
    event ThresholdUpdated(uint256 oldThreshold, uint256 newThreshold);

    modifier onlyAdmin() {
        require(msg.sender == admin, "only admin");
        _;
    }

    constructor(uint256 _threshold) public {
        require(_threshold > 0, "threshold must > 0");
        admin = msg.sender;
        threshold = _threshold;
    }

    /// @notice 提交对某节点的恶意行为指控
    /// @param _nodeId  被指控节点的标识 (如 "routing_node3")
    /// @param _reason  指控原因描述
    function accuse(string memory _nodeId, string memory _reason) public {
        require(!hasAccused[_nodeId][msg.sender], "already accused by this accuser");

        Accusation memory acc = Accusation({
            accuser: msg.sender,
            nodeId: _nodeId,
            reason: _reason,
            timestamp: block.timestamp
        });

        accusations[_nodeId].push(acc);
        hasAccused[_nodeId][msg.sender] = true;
        accuserCount[_nodeId] += 1;

        emit NodeAccused(_nodeId, msg.sender, _reason, block.timestamp);

        // 达到阈值自动拉黑
        if (accuserCount[_nodeId] >= threshold && nodeStatus[_nodeId] == NodeStatus.ACTIVE) {
            nodeStatus[_nodeId] = NodeStatus.BLOCKED;
            emit NodeBlocked(_nodeId, accuserCount[_nodeId], block.timestamp);
        }
    }

    /// @notice 查询节点状态
    /// @param _nodeId 节点标识
    /// @return status "ACTIVE" 或 "BLOCKED"
    function queryStatus(string memory _nodeId) public view returns (string memory status) {
        if (nodeStatus[_nodeId] == NodeStatus.BLOCKED) {
            return "BLOCKED";
        }
        return "ACTIVE";
    }

    /// @notice 查询节点的所有指控记录数量
    function getAccusationCount(string memory _nodeId) public view returns (uint256) {
        return accusations[_nodeId].length;
    }

    /// @notice 管理员手动解除拉黑
    function unblock(string memory _nodeId) public onlyAdmin {
        require(nodeStatus[_nodeId] == NodeStatus.BLOCKED, "node is not blocked");
        nodeStatus[_nodeId] = NodeStatus.ACTIVE;
        emit NodeUnblocked(_nodeId, block.timestamp);
    }

    /// @notice 管理员更新阈值
    function setThreshold(uint256 _newThreshold) public onlyAdmin {
        require(_newThreshold > 0, "threshold must > 0");
        emit ThresholdUpdated(threshold, _newThreshold);
        threshold = _newThreshold;
    }
}
