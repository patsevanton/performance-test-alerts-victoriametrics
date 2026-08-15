terraform {
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "~> 0.220"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "3.2.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.14.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "2.9.0"
    }
  }
  required_version = ">= 1.3"
}
