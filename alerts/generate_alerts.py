import yaml
import datetime
import random

def generate_alert(alert_index, vmrule_index):
    """Генерирует один алерт"""
    severities = ["info", "warning", "critical"]
    severity = random.choice(severities)

    alert_name = f"LoadTestAlert_{vmrule_index}_{alert_index+1}"
    alert_id = f"loadtest_{vmrule_index}_{str(alert_index+1).zfill(3)}"

    rule = {
        "alert": alert_name,
        "expr": "vector(1)",  # Всегда истинное выражение
        "for": f"{random.choice([0, 5, 10, 15, 30])}s",
        "labels": {
            "severity": severity,
            "test": "loadtest",
            "alert_type": "always_firing",
            "alert_id": alert_id,
            "vmrule_group": f"group-{vmrule_index}"
        },
        "annotations": {
            "summary": f"Load Test Alert {vmrule_index}-{alert_index+1} - {severity.capitalize()}",
            "description": f"This is load testing alert from VMRule {vmrule_index}. Generated at {datetime.datetime.now().isoformat()}",
            "test_iteration": f"{vmrule_index}-{alert_index+1}"
        }
    }
    return rule

def generate_vmrule(vmrule_index, num_alerts_in_group):
    """Генерирует один VMRule с указанным количеством алертов"""
    rules_group = {
        "apiVersion": "operator.victoriametrics.com/v1beta1",
        "kind": "VMRule",
        "metadata": {
            "name": f"loadtest-alerts-{vmrule_index}",
            "labels": {
                "app": "alertmanager-loadtest",
                "test-type": "always-firing",
                "vmrule-index": str(vmrule_index)
            }
        },
        "spec": {
            "groups": [
                {
                    "name": f"loadtest-generated-{vmrule_index}",
                    "interval": "30s",
                    "rules": [generate_alert(i, vmrule_index) for i in range(num_alerts_in_group)]
                }
            ]
        }
    }
    return rules_group

def main():
    # Конфигурация
    num_vmrules = 2  # Количество VMRule объектов
    alerts_per_vmrule = 2  # Количество алертов в каждом VMRule
    
    all_vmrules = []
    
    # Генерируем VMRule объекты
    for vmrule_idx in range(1, num_vmrules + 1):
        vmrule = generate_vmrule(vmrule_idx, alerts_per_vmrule)
        all_vmrules.append(vmrule)
    
    # Записываем в YAML с разделителем "---" между документами
    with open("loadtest-vmrules.yaml", "w") as f:
        for i, vmrule in enumerate(all_vmrules):
            yaml.dump(vmrule, f, sort_keys=False, default_flow_style=False)
            if i < len(all_vmrules) - 1:
                f.write("\n---\n")  # Разделитель YAML документов
    
    print(f"loadtest-vmrules.yaml успешно создан!")
    print(f"Всего VMRule: {num_vmrules}")
    print(f"Алертов в каждом VMRule: {alerts_per_vmrule}")
    print(f"Всего алертов: {num_vmrules * alerts_per_vmrule}")
    print("\nСозданные VMRule и алерты:")
    
    for vmrule_idx in range(1, num_vmrules + 1):
        print(f"\nVMRule {vmrule_idx} (loadtest-alerts-{vmrule_idx}):")
        for alert_idx in range(1, alerts_per_vmrule + 1):
            print(f"  - LoadTestAlert_{vmrule_idx}_{alert_idx}")

if __name__ == "__main__":
    main()