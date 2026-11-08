# Ресурс для создания сети VPC в Yandex Cloud
resource "yandex_vpc_network" "vmalert" {
  name = "vpc" # Имя сети VPC
}

# Подсеть в зоне "ru-central1-a".
# Рабочие зоны — только ru-central1-a и ru-central1-d: ru-central1-b выключена
# ("zone ... is disabled. No new resources could be allocated"), в ru-central1-e
# нет свободных ресурсов, ru-central1-k недоступна.
# CIDR 10.0.1.0/24 в ru-central1-a уже занят подсетью elastic-chaos-a в этом же
# облаке (проверка YC «Subnet CIDRs can not overlap» смотрит не только на сеть),
# поэтому берём 10.0.5.0/24.
resource "yandex_vpc_subnet" "vmalert-a" {
  v4_cidr_blocks = ["10.0.5.0/24"]               # CIDR блок для подсети (IP-диапазон)
  zone           = "ru-central1-a"               # Зона, где будет размещена подсеть
  network_id     = yandex_vpc_network.vmalert.id # ID сети, к которой будет привязана подсеть
  route_table_id = yandex_vpc_route_table.rt.id  # Маршрутизация исходящего трафика через NAT-шлюз
}

# Подсеть в зоне "ru-central1-d"
resource "yandex_vpc_subnet" "vmalert-d" {
  v4_cidr_blocks = ["10.0.2.0/24"]               # CIDR блок для подсети (IP-диапазон)
  zone           = "ru-central1-d"               # Зона, где будет размещена подсеть
  network_id     = yandex_vpc_network.vmalert.id # ID сети, к которой будет привязана подсеть
  route_table_id = yandex_vpc_route_table.rt.id  # Маршрутизация исходящего трафика через NAT-шлюз
}

# Подсеть в зоне "ru-central1-e".
# Региональный master требует ровно три зоны, а рабочих зон для нод сейчас две
# (ru-central1-a и ru-central1-d). Мастер в ru-central1-e создаётся — так же
# развёрнуты остальные кластеры этого облака, — но ноды в эту зону не ставим:
# свободных ресурсов для них там нет.
resource "yandex_vpc_subnet" "vmalert-e" {
  v4_cidr_blocks = ["10.0.3.0/24"]               # CIDR блок для подсети (IP-диапазон)
  zone           = "ru-central1-e"               # Зона, где будет размещена подсеть
  network_id     = yandex_vpc_network.vmalert.id # ID сети, к которой будет привязана подсеть
  route_table_id = yandex_vpc_route_table.rt.id  # Маршрутизация исходящего трафика через NAT-шлюз
}

# Публичный IP-адрес для NAT-шлюза
resource "yandex_vpc_address" "nat" {
  name = "nat-pip"
  external_ipv4_address {
    zone_id = yandex_vpc_subnet.vmalert-a.zone
  }
}

# NAT-шлюз для исходящего трафика из приватных подсетей
resource "yandex_vpc_gateway" "nat" {
  name = "nat-gw"
  shared_egress_gateway {}
}

# Таблица маршрутизации: весь исходящий трафик (0.0.0.0/0) направляем через NAT-шлюз
resource "yandex_vpc_route_table" "rt" {
  name       = "rt-nat"
  network_id = yandex_vpc_network.vmalert.id
  static_route {
    destination_prefix = "0.0.0.0/0"
    gateway_id         = yandex_vpc_gateway.nat.id
  }
}
