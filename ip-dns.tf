# Создание внешнего IP-адреса в Yandex Cloud
resource "yandex_vpc_address" "addr" {
  name = "vmalert-pip" # Имя ресурса внешнего IP-адреса

  external_ipv4_address {
    zone_id = local.subnet_e_zone # Зона доступности, где будет выделен IP-адрес
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
