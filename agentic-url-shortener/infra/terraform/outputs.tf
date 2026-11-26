output "service_url" {
  description = "Public Cloud Run service URL."
  value       = google_cloud_run_v2_service.url_shortener.uri
}

output "artifact_registry_repository" {
  description = "Artifact Registry repository URL."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "image_uri" {
  description = "Container image currently configured for Cloud Run."
  value       = var.image
}
