# Создание внешнего IP-адреса в Yandex Cloud
resource "yandex_vpc_address" "addr" {
  name = "vmalert-pip"  # Имя ресурса внешнего IP-адреса

  external_ipv4_address {
    zone_id = yandex_vpc_subnet.vmalert-a.zone  # Зона доступности, где будет выделен IP-адрес
  }
}

# Создание публичной DNS-зоны в Yandex Cloud DNS
resource "yandex_dns_zone" "apatsev-org-ru" {
  name  = "apatsev-org-ru-zone"  # Имя ресурса DNS-зоны

  zone  = "apatsev.org.ru."      # Доменное имя зоны (с точкой в конце)
  public = true                  # Указание, что зона является публичной

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

resource "yandex_dns_recordset" "chaos_dashboard" {
  zone_id = yandex_dns_zone.apatsev-org-ru.id
  name    = "chaos-dashboard.apatsev.org.ru."
  type    = "A"
  ttl     = 200
  data    = [yandex_vpc_address.addr.external_ipv4_address[0].address]
}
