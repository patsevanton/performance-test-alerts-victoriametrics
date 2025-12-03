import yaml
import datetime
import random
import os


def generate_alert(alert_index, vmrule_index):
    """Генерирует один алерт"""
    severities = ["info", "warning", "critical"]
    severity = random.choice(severities)

    alert_name = f"LoadTestAlert_{vmrule_index}_{alert_index+1}"
    alert_id = f"loadtest_{vmrule_index}_{str(alert_index+1).zfill(4)}"

    return {
        "alert": alert_name,
        "expr": "vector(1)",
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
            "description": (
                f"This is load testing alert from VMRule {vmrule_index}. "
                f"Generated at {datetime.datetime.now().isoformat()}"
            ),
            "test_iteration": f"{vmrule_index}-{alert_index+1}"
        }
    }


def generate_vmrule(vmrule_index, num_alerts_in_group):
    """Генерирует один VMRule с указанным количеством алертов"""
    return {
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
                    "rules": [
                        generate_alert(i, vmrule_index)
                        for i in range(num_alerts_in_group)
                    ]
                }
            ]
        }
    }


def main():
    # ⚠️ Настройки
    num_vmrules = 1000
    alerts_per_vmrule = 1000

    output_dir = "vmrules"
    os.makedirs(output_dir, exist_ok=True)

    print("Генерация YAML файлов...")
    for vmrule_idx in range(1, num_vmrules + 1):
        vmrule = generate_vmrule(vmrule_idx, alerts_per_vmrule)

        file_path = os.path.join(output_dir, f"vmrule-{vmrule_idx}.yaml")

        with open(file_path, "w") as f:
            yaml.dump(vmrule, f, sort_keys=False, default_flow_style=False)

        if vmrule_idx % 50 == 0:
            print(f"Создано файлов: {vmrule_idx}/{num_vmrules}")

    print("\nГенерация завершена!")
    print(f"Каталог: {output_dir}")
    print(f"Всего файлов: {num_vmrules}")
    print(f"Алертов в каждом VMRule: {alerts_per_vmrule}")
    print(f"Общее количество алертов: {num_vmrules * alerts_per_vmrule}")


if __name__ == "__main__":
    main()
