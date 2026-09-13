"""一次性校准：用 README 权威标题/描述修正 KB 与 Zotero 的论文认知（保证认知准确性）。
运行：python -m service.reconcile_truth
"""
import re
from pathlib import Path

from pyzotero import zotero as _zot

from .config_loader import Config
from .manifest import Manifest

# pid -> 修正字段。含 core_zh 则整段重写；否则仅改 title/venue。
CORRECTIONS = {
    "local:Salus_FineGrained_GPU_Sharing_MLSys2019": {
        "title_en": "Salus: Fine-Grained GPU Sharing Primitives for Deep Learning Applications",
    },
    "local:FaaShare_SLO_Aware_GPU_Sharing_Serverless_TPDS": {
        "title_en": "FaaShare: Enabling Generic Low-Latency and SLO-Aware GPU Sharing in Serverless Computing",
        "venue": "TPDS",
    },
    "2407.13088": {  # Wise
        "venue": "IWQoS",
    },
    "local:REEF_Preemption_DNN_Inference_OSDI2022": {
        "title_en": "Microsecond-scale Preemption for Concurrent GPU-accelerated DNN Inferences",
    },
    "local:Shepherd_Serving_DNNs_Wild_NSDI2023": {
        "title_en": "Shepherd: Serving DNNs in the Wild",
    },
    "local:Gimbal_Taming_Flexible_Job_Packing_TACO2025": {
        "title_en": "Taming Flexible Job Packing in Deep Learning Training Clusters",
    },
    "local:Paragon_QoS_Aware_Scheduling_ASPLOS2013": {
        "title_en": "Paragon: QoS-Aware Scheduling for Heterogeneous Datacenters",
    },
    "local:Gavel_Heterogeneity_Aware_Scheduling_OSDI2020": {
        "title_en": "Gavel: Heterogeneity-Aware Cluster Scheduling for Deep Learning Workloads",
    },
    "local:Survey_DL_Workload_Scheduling_GPU_Datacenters_CSUR2024": {
        "title_en": "Deep Learning Workload Scheduling in GPU Datacenters: A Survey",
    },
    "local:Borg_Large_Scale_Cluster_Management_EuroSys2015": {
        "title_en": "Large-scale Cluster Management at Google with Borg",
    },

    # —— 以下 5 篇核心内容重写 ——
    "local:Clockwork_Predictable_Inference_OSDI2020": {
        "title_en": "Serving DNNs like Clockwork: Performance Predictability from the Bottom Up",
        "title_zh": "Clockwork：自底向上的可预测 DNN 推理服务",
        "core_zh": "Clockwork 提出一种让 DNN 推理服务像时钟一样可预测执行的框架：通过确定性调度，每个模型实例以固定周期、固定批大小处理请求，底层 GPU 资源分配与执行时间事先确定且可复现，使端到端延迟能被精确预测。它结合请求去重与静态资源规划，在保证延迟可预测的前提下提升吞吐。",
        "core_en": "Clockwork makes DNN inference serving predictable 'like clockwork': through deterministic scheduling, each model instance processes requests on a fixed period with a fixed batch size, and low-level GPU allocation and execution times are decided in advance and reproducible, so end-to-end latency can be predicted precisely. Combined with request deduplication and static resource planning, it improves throughput while keeping latency predictable.",
        "sig_zh": "该工作确立了「用确定性换取可预测性」的推理服务范式，为严格 SLO 场景下的延迟控制提供了自底向上的系统设计思路。",
        "sig_en": "This work establishes a 'determinism for predictability' serving paradigm, offering a bottom-up systems design for latency control under strict SLOs.",
    },
    "local:Quasar_Resource_Efficient_QoS_Aware_ASPLOS2014": {
        "title_en": "Quasar: Resource-Efficient and QoS-Aware Cluster Management",
        "title_zh": "Quasar：资源高效、QoS 感知的集群管理",
        "core_zh": "Quasar 是面向通用数据中心的资源高效、QoS 感知的集群管理系统（并非机器学习专用）。它在线刻画应用在不同资源配置与干扰条件下的性能与敏感度，据此为任意工作负载分配「刚好满足 QoS 所需」的最小资源，从而在保障延迟等 SLO 的同时显著提升资源利用率与调度灵活性。",
        "core_en": "Quasar is a resource-efficient, QoS-aware cluster management system for general datacenters (not ML-specific). It online-profiles application performance and interference sensitivity under different resource allocations, then assigns each workload the minimal resources needed to meet its QoS, improving utilization and scheduling flexibility while preserving SLOs such as latency.",
        "sig_zh": "与 Paragon 同源（Delimitrou & Kozyrakis），是 CPU 数据中心 QoS 感知调度的经典之作，其「按需最小资源分配」思想深刻影响了后续资源调度研究。",
        "sig_en": "A classic of QoS-aware scheduling for CPU datacenters (same authors as Paragon), its 'allocate just enough resources to meet QoS' idea profoundly influenced later resource-scheduling research.",
    },
    "local:AntMan_CoLocation_DL_Training_OSDI2020": {
        "title_en": "AntMan: Dynamic Scaling on GPU Clusters for Deep Learning",
        "title_zh": "AntMan：深度学习 GPU 集群的动态伸缩",
        "core_zh": "AntMan 是面向深度学习 GPU 集群的动态伸缩系统，核心是让在线推理与离线训练两类负载在 GPU 上安全共置：通过在线服务的显存协调器与离线训练的弹性执行器联动，训练任务在在线流量波谷时动态扩容填充空闲算力、在波峰时缩容让出资源，从而在不影响在线服务质量的前提下显著提升集群利用率。",
        "core_en": "AntMan is a dynamic-scaling system for deep learning GPU clusters that safely co-locates online inference and offline training: by coordinating the online service's memory coordinator with the training job's elastic executor, training scales up to fill idle GPU capacity during traffic valleys and scales down during peaks, substantially improving cluster utilization without hurting online quality of service.",
        "sig_zh": "该工作展示了在线/离线动态伸缩共置的可行性，其显存协调与弹性执行机制是「预测波谷填充 GPU 卡池」课题的直接技术基础。",
        "sig_en": "This work demonstrates the feasibility of elastic online/offline co-location, and its memory-coordination and elastic-execution mechanisms are a direct technical foundation for predictive valley-based GPU capacity filling.",
    },
    "local:Sirius_Colocating_ML_Inference_and_Training_ATC2025": {
        "title_en": "Colocating ML Inference and Training with Fast GPU Memory Handover",
        "title_zh": "Sirius：通过快速 GPU 内存移交共置推理与训练",
        "core_zh": "Sirius 提出在 GPU 上共置机器学习推理与训练的方法，核心机制是「快速 GPU 内存移交」（fast GPU memory handover）：在推理与训练任务之间高效传递 GPU 内存状态，使训练任务能无缝复用在线推理空闲的显存与算力，从而在保障推理延迟 SLO 的同时提升 GPU 利用率。",
        "core_en": "Sirius colocates ML inference and training on GPUs via fast GPU memory handover: it efficiently hands over GPU memory state between inference and training tasks so that training can seamlessly reuse the memory and compute idled by online inference, improving GPU utilization while meeting inference latency SLOs.",
        "sig_zh": "该工作为推理+训练共置提供了「内存快速移交」这一关键机制，直接切合在线任务与离线训练共享 GPU 卡池的核心诉求。",
        "sig_en": "This work contributes fast memory handover as a key mechanism for inference+training co-location, directly addressing the core need of sharing GPU capacity between online serving and offline training.",
    },
    "local:Gandiva_Introspective_Cluster_OSDI2018": {
        "title_en": "Gandiva: Introspective Cluster Scheduling for Deep Learning",
        "title_zh": "Gandiva：面向深度学习的内省式集群调度",
        "core_zh": "Gandiva 是面向深度学习训练的内省式集群调度系统，核心机制包括：通过运行时内省监控 GPU 利用情况，对 GPU 做时间片切分让多作业分时共享、并将多作业打包到同一 GPU；当作业与硬件匹配不佳时，通过检查点/恢复实现作业跨 GPU 迁移，从而减少碎片、提升集群利用率。",
        "core_en": "Gandiva is an introspective cluster scheduling system for deep learning training. Its key mechanisms include runtime introspection to monitor GPU utilization, time-slicing GPUs so multiple jobs share them, packing multiple jobs onto one GPU, and checkpoint/resume-based job migration when a job is poorly matched to its hardware, reducing fragmentation and improving cluster utilization.",
        "sig_zh": "该工作为训练集群提供了「时间片+打包+迁移」的内省式调度范式，其检查点迁移机制是 GPU 卡池填充中 suspend/resume 的重要参考。",
        "sig_en": "This work offers an introspective scheduling paradigm of time-slicing, packing, and migration for training clusters; its checkpoint-based migration is an important reference for suspend/resume in GPU capacity filling.",
    },
}


def main():
    cfg = Config()
    root = cfg.root
    man = Manifest(root / "knowledge-base" / "_manifest.json")
    z = _zot.Zotero(cfg.zotero_user_id, "user", cfg.zotero_api_key)

    for pid, c in CORRECTIONS.items():
        entry = man.get(pid)
        if not entry:
            print(f"[skip] manifest 无 {pid}")
            continue
        area_zh = (entry.get("area") or "").split("::")[-1]
        work = (entry.get("work_slugs") or ["uncategorized"])[0]
        kb_path = root / "knowledge-base" / "fields" / area_zh / work / f"{Path(entry['path']).stem}.md"

        # 1. Zotero 标题
        if c.get("title_en") and entry.get("zotero_key"):
            try:
                it = z.item(entry["zotero_key"])
                it["data"]["title"] = c["title_en"]
                z.update_item(it)
            except Exception as e:
                print(f"[warn] Zotero 标题 {pid}: {e}")

        # 2. KB 文件
        head, fm, tail = kb_path.read_text(encoding="utf-8").split("---", 2)
        if c.get("title_en"):
            fm = re.sub(r'title_en: ".*"', f'title_en: "{c["title_en"]}"', fm)
        if c.get("title_zh"):
            fm = re.sub(r'title_zh: ".*"', f'title_zh: "{c["title_zh"]}"', fm)
        if c.get("venue"):
            fm = re.sub(r"venue: .*", f"venue: {c['venue']}", fm)
        if "core_zh" in c:
            body = (f"## 核心内容（中文）\n{c['core_zh']}\n\n"
                    f"## Core idea (English)\n{c['core_en']}\n\n"
                    f"## 意义（中文）\n{c['sig_zh']}\n\n"
                    f"## Significance (English)\n{c['sig_en']}\n")
            kb_path.write_text("---" + fm + "---\n\n" + body, encoding="utf-8")
        else:
            kb_path.write_text("---" + fm + "---" + tail, encoding="utf-8")

        # 3. manifest 标题
        if c.get("title_en"):
            entry["title"] = c["title_en"]

    man.save()
    print("reconcile done")


if __name__ == "__main__":
    main()
