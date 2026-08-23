locals {
  network_id = yandex_vpc_network.vmalert.id

  subnet_b_id   = yandex_vpc_subnet.vmalert-b.id
  subnet_d_id   = yandex_vpc_subnet.vmalert-d.id
  subnet_e_id   = yandex_vpc_subnet.vmalert-e.id
  subnet_b_zone = yandex_vpc_subnet.vmalert-b.zone
  subnet_d_zone = yandex_vpc_subnet.vmalert-d.zone
  subnet_e_zone = yandex_vpc_subnet.vmalert-e.zone

  # Публичный IP балансировщика ingress-nginx. FQDN сервисов формируются через sslip.io
  # из этого адреса (см. outputs в k8s.tf и шаблоны *.tftpl).
  ingress_public_ip = yandex_vpc_address.addr.external_ipv4_address[0].address
}
