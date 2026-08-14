# Создание внешнего IP-адреса в Yandex Cloud
resource "yandex_vpc_address" "addr" {
  name = "vmalert-pip" # Имя ресурса внешнего IP-адреса

  external_ipv4_address {
    zone_id = yandex_vpc_subnet.vmalert-e.zone # Зона доступности, где будет выделен IP-адрес
  }
}

# Пауза перед удалением публичного IP-адреса при terraform destroy.
# LoadBalancer, создаваемый cloud-controller-manager через Service ingress-nginx,
# освобождает адрес не мгновенно после удаления кластера/helm-релиза — без паузы
# yandex_vpc_address.addr падает с ошибкой "Address in use".
# Порядок destroy: helm_release -> cluster -> time_sleep (пауза) -> yandex_vpc_address.addr.
resource "time_sleep" "wait_lb_release" {
  destroy_duration = "60s"

  depends_on = [
    yandex_vpc_address.addr,
  ]
}

# Создание публичной DNS-зоны в Yandex Cloud DNS
resource "yandex_dns_zone" "apatsev-org-ru" {
  name = "apatsev-org-ru-zone" # Имя ресурса DNS-зоны

  zone   = "apatsev.org.ru." # Доменное имя зоны (с точкой в конце)
  public = true              # Указание, что зона является публичной

  # Привязка зоны к VPC-сети, чтобы можно было использовать приватный DNS внутри сети
  private_networks = [yandex_vpc_network.vmalert.id]
}

# Создание DNS-записи типа A, указывающей на внешний IP
resource "yandex_dns_recordset" "grafana" {
  zone_id = yandex_dns_zone.apatsev-org-ru.id
  name    = "grafana.apatsev.org.ru."
  type    = "A"
  ttl     = 200
  data    = [yandex_vpc_address.addr.external_ipv4_address[0].address]
}

resource "yandex_dns_recordset" "victorialogs" {
  zone_id = yandex_dns_zone.apatsev-org-ru.id
  name    = "victorialogs.apatsev.org.ru."
  type    = "A"
  ttl     = 200
  data    = [yandex_vpc_address.addr.external_ipv4_address[0].address]
}

resource "yandex_dns_recordset" "victoriametrics" {
  zone_id = yandex_dns_zone.apatsev-org-ru.id
  name    = "vmselect.apatsev.org.ru."
  type    = "A"
  ttl     = 200
  data    = [yandex_vpc_address.addr.external_ipv4_address[0].address]
}
