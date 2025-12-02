import yaml
import datetime
import random

def generate_alert(index):
    # Возможные уровни severity
    severities = ["info", "warning", "critical"]
    severity = random.choice(severities)

    alert_name = f"LoadTestAlert{index+1}"
    alert_id = f"loadtest_{str(index+1).zfill(3)}"

    rule = {
        "alert": alert_name,
        "expr": "vector(1)",  # Всегда истинное выражение
        "for": f"{random.choice([0, 5, 10, 15, 30])}s",  # Случайное время ожидания
        "labels": {
            "severity": severity,
            "test": "loadtest",
            "alert_type": "always_firing",
            "alert_id": alert_id
        },
        "annotations": {
            "summary": f"Load Test Alert {index+1} - {severity.capitalize()} level",
            "description": f"This is a load testing alert that always fires. Generated at {datetime.datetime.now().isoformat()}",
            "test_iteration": str(index+1)
        }
    }
    return rule

def main():
    num_alerts = 10  # Количество алертов
    rules_group = {
        "apiVersion": "operator.victoriametrics.com/v1beta1",
        "kind": "VMRule",
        "metadata": {
            "name": "loadtest-alerts",
            "labels": {
                "app": "alertmanager-loadtest",
                "test-type": "always-firing"
            }
        },
        "spec": {
            "groups": [
                {
                    "name": "loadtest-generated",
                    "interval": "30s",
                    "rules": [generate_alert(i) for i in range(num_alerts)]
                }
            ]
        }
    }

    # Записываем в YAML
    with open("loadtest-vmrules.yaml", "w") as f:
        yaml.dump(rules_group, f, sort_keys=False, default_flow_style=False)

    print(f"loadtest-vmrules.yaml успешно создан с {num_alerts} алертами!")
    for i in range(num_alerts):
        print(f"- LoadTestAlert{i+1}")

if __name__ == "__main__":
    main()
