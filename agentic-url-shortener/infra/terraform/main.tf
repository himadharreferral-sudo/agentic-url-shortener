locals {
  required_apis = toset([
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "run.googleapis.com",
  ])
}

resource "google_project_service" "required" {
  for_each = local.required_apis

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_artifact_registry_repository" "images" {
  project       = var.project_id
  location      = var.region
  repository_id = var.repository_id
  description   = "Container images for the URL shortener"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "runtime" {
  project      = var.project_id
  account_id   = "url-shortener-run"
  display_name = "URL Shortener Cloud Run runtime"

  depends_on = [google_project_service.required]
}

resource "google_cloud_run_v2_service" "url_shortener" {
  project  = var.project_id
  name     = var.service_name
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = 1

    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "HOST"
        value = "0.0.0.0"
      }

      env {
        name  = "PORT"
        value = "8080"
      }

      env {
        name  = "DB_PATH"
        value = "/tmp/shortener.db"
      }

      dynamic "env" {
        for_each = var.public_base_url == "" ? [] : [var.public_base_url]

        content {
          name  = "PUBLIC_BASE_URL"
          value = env.value
        }
      }
    }
  }

  depends_on = [
    google_project_service.required,
    google_artifact_registry_repository.images,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  count = var.allow_unauthenticated ? 1 : 0

  project  = var.project_id
  location = google_cloud_run_v2_service.url_shortener.location
  name     = google_cloud_run_v2_service.url_shortener.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
