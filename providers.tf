provider "yandex" {
  # Все ресурсы без явного folder_id создаются в этом folder.
  # Без явного указания провайдер берёт folder из YC_FOLDER_ID,
  # который может указывать на несуществующий folder.
  folder_id = var.folder_id
}
