# Создание сервисного аккаунта для управления Kubernetes
resource "yandex_iam_service_account" "sa_k8s_editor" {
  folder_id = local.folder_id
  name      = "sa-k8s-editor" # Имя сервисного аккаунта
}

# Назначение роли "editor" сервисному аккаунту на уровне папки
resource "yandex_resourcemanager_folder_iam_member" "sa_k8s_editor_permissions" {
  role      = "editor" # Роль, дающая полные права на ресурсы папки
  folder_id = local.folder_id
  member    = "serviceAccount:${yandex_iam_service_account.sa_k8s_editor.id}" # Назначаемый участник
}

# Пауза, чтобы изменения IAM успели примениться до создания кластера
resource "time_sleep" "wait_sa" {
  create_duration = "20s"
  depends_on = [
    yandex_iam_service_account.sa_k8s_editor,
    yandex_resourcemanager_folder_iam_member.sa_k8s_editor_permissions
  ]
}

# Создание Kubernetes-кластера в Yandex Cloud
resource "yandex_kubernetes_cluster" "vmalert" {
  name       = "vmalert" # Имя кластера
  folder_id  = local.folder_id
  network_id = local.network_id # Сеть, к которой подключается кластер

  master {
    version = "1.33" # Версия Kubernetes мастера
    zonal {
      zone      = local.subnet_e_zone # Зона размещения мастера
      subnet_id = local.subnet_e_id   # Подсеть для мастера
    }

    public_ip = true # Включение публичного IP для доступа к мастеру
  }

  # Сервисный аккаунт для управления кластером и нодами
  service_account_id      = yandex_iam_service_account.sa_k8s_editor.id
  node_service_account_id = yandex_iam_service_account.sa_k8s_editor.id

  release_channel = "STABLE" # Канал обновлений

  # Зависимость от ожидания применения IAM-ролей.
  # При destroy кластер должен удалиться ДО time_sleep.wait_lb_release (пауза перед освобождением IP),
  # чтобы cloud-controller-manager успел снять LoadBalancer с адреса yandex_vpc_address.addr.
  depends_on = [
    time_sleep.wait_sa,
    time_sleep.wait_lb_release,
  ]
}

# Группа узлов для Kubernetes-кластера
resource "yandex_kubernetes_node_group" "k8s_node_group" {
  description = "Node group for the Managed Service for Kubernetes cluster"
  name        = "k8s-node-group"
  cluster_id  = yandex_kubernetes_cluster.vmalert.id
  version     = "1.33" # Версия Kubernetes на нодах

  scale_policy {
    fixed_scale {
      # kubectl показал Pending у vmalert из-за `Insufficient cpu` (request=4 core на pod, на части нод уже ~96% по requests).
      # Добавляем 1 ноду для гарантированного размещения тяжёлых monitoring pod'ов при rollout/reconcile.
      size = 23 # увеличить кол-во нод
    }
  }

  allocation_policy {
    # Распределение нод по зонам отказоустойчивости
    location { zone = local.subnet_b_zone }
    location { zone = local.subnet_d_zone }
    location { zone = local.subnet_e_zone }
  }

  instance_template {
    # Переходим на standard-v3: более современная платформа, лучше подходит под текущий CPU-bound профиль кластера.
    platform_id = "standard-v3"

    # Используем preemptible-ноды для снижения стоимости стенда.
    scheduling_policy {
      preemptible = true
    }

    network_interface {
      nat = false # Публичные IP на нодах выключены; исходящий трафик через NAT-шлюз (см. net.tf)
      subnet_ids = [
        local.subnet_b_id,
        local.subnet_d_id,
        local.subnet_e_id
      ]
    }

    resources {
      # 8 vCPU / 8GB на ноду. Bottleneck по памяти (requests RAM на ноде ~99% при 64Mi/app),
      # снимается снижением request app до 32Mi + scale до 16 нод.
      cores  = 4 # vCPU # уменьшить
      memory = 8 # ГБ # уменьшить
    }

    boot_disk {
      type = "network-hdd" # Тип диска
      size = 30            # Размер диска
    }
  }
}

# Настройка провайдера Helm для установки чарта в Kubernetes
provider "helm" {
  kubernetes = {
    host                   = yandex_kubernetes_cluster.vmalert.master[0].external_v4_endpoint   # Адрес API Kubernetes
    cluster_ca_certificate = yandex_kubernetes_cluster.vmalert.master[0].cluster_ca_certificate # CA-сертификат
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      args        = ["k8s", "create-token"] # Команда получения токена через CLI Yandex.Cloud
      command     = "yc"
    }
  }
}

# Установка ingress-nginx через Helm
resource "helm_release" "ingress_nginx" {
  name             = "ingress-nginx"
  chart            = "oci://cr.yandex/yc-marketplace/yandex-cloud/ingress-nginx/chart/ingress-nginx"
  version          = "4.13.0"
  namespace        = "ingress-nginx"
  create_namespace = true

  depends_on = [
    yandex_kubernetes_cluster.vmalert,
    yandex_kubernetes_node_group.k8s_node_group,
    time_sleep.wait_lb_release,
  ]

  values = [
    yamlencode({
      controller = {
        service = {
          loadBalancerIP = local.ingress_public_ip
        }
        config = {
          log-format-escape-json = "true"
          log-format-upstream = trimspace(<<-EOT
            {"ts":"$time_iso8601","http":{"request_id":"$req_id","method":"$request_method","status_code":$status,"url":"$host$request_uri","host":"$host","uri":"$request_uri","request_time":$request_time,"user_agent":"$http_user_agent","protocol":"$server_protocol","trace_session_id":"$http_trace_session_id","server_protocol":"$server_protocol","content_type":"$sent_http_content_type","bytes_sent":"$bytes_sent"},"nginx":{"x-forward-for":"$proxy_add_x_forwarded_for","remote_addr":"$proxy_protocol_addr","http_referrer":"$http_referer"}}
          EOT
          )
        }
      }
    })
  ]
}

# Вывод команды для получения kubeconfig
output "k8s_cluster_credentials_command" {
  value = "yc managed-kubernetes cluster get-credentials --id ${yandex_kubernetes_cluster.vmalert.id} --external --force"
}

output "ingress_public_ip" {
  description = "External ingress-nginx IP"
  value       = local.ingress_public_ip
}

output "grafana_url" {
  description = "URL Grafana (сформирован через sslip.io из публичного IP балансировщика ingress-nginx)"
  value       = "http://grafana.${local.ingress_public_ip}.sslip.io"
}

output "grafana_admin_password_command" {
  description = "Команда для получения пароля администратора Grafana из секрета vmks-grafana"
  value       = "kubectl get secret vmks-grafana -n vmks -o jsonpath='{.data.admin-password}' | base64 --decode; echo"
}

output "vmselect_url" {
  description = "URL vmselect / VictoriaMetrics query API (сформирован через sslip.io из публичного IP балансировщика ingress-nginx)"
  value       = "http://vmselect.${local.ingress_public_ip}.sslip.io"
}

output "victorialogs_url" {
  description = "URL VictoriaLogs (сформирован через sslip.io из публичного IP балансировщика ingress-nginx)"
  value       = "http://victorialogs.${local.ingress_public_ip}.sslip.io"
}
