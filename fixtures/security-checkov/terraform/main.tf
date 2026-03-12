resource "aws_s3_bucket" "public_bucket" {
  bucket = "checkov-insecure-example"
  acl    = "public-read"
}
