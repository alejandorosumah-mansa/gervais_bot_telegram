import os
import boto3
from datetime import datetime
from botocore.exceptions import ClientError
import logging
import json

class S3Helper:
    def __init__(self, bucket_name):
        logging.info(f"Initializing S3Helper with bucket: {bucket_name}")
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
            region_name=os.getenv('AWS_REGION', 'us-east-1')
        )
        self.bucket_name = bucket_name
        self._verify_bucket()

    def _verify_bucket(self):
        """Verify the bucket exists and we have access to it"""
        try:
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            logging.info(f"Successfully verified access to bucket: {self.bucket_name}")
        except Exception as e:
            logging.error(f"Failed to verify S3 bucket {self.bucket_name}: {str(e)}")
            raise

    def upload_image(self, image_data, user_id, original_filename=None, metadata=None):
        """
        Upload an image to S3 in a folder named after the user_id
        Args:
            image_data: The image bytes
            user_id: Telegram user ID
            original_filename: Original filename if available
            metadata: Dictionary containing image metadata (location, etc.)
        """
        try:
            # Generate filename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            if original_filename:
                extension = os.path.splitext(original_filename)[1]
            else:
                extension = '.jpg'  # default extension
            
            filename = f"{timestamp}{extension}"
            s3_key = f"{user_id}/{filename}"
            
            # Create metadata object for S3
            s3_metadata = {}
            if metadata:
                # Convert all metadata values to strings as required by S3
                s3_metadata = {k: str(v) for k, v in metadata.items() if v is not None}
                
                # Save full metadata as JSON file
                metadata_key = f"{user_id}/{timestamp}_metadata.json"
                self.s3_client.put_object(
                    Bucket=self.bucket_name,
                    Key=metadata_key,
                    Body=json.dumps(metadata, indent=2),
                    ContentType='application/json'
                )
            
            logging.info(f"Attempting to upload image to S3 - Bucket: {self.bucket_name}, Key: {s3_key}")
            logging.info(f"Image metadata: {s3_metadata}")
            
            # Upload the image with metadata
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=image_data,
                ContentType='image/jpeg',
                Metadata=s3_metadata
            )
            
            logging.info(f"Successfully uploaded image to S3: s3://{self.bucket_name}/{s3_key}")
            return f"s3://{self.bucket_name}/{s3_key}"
        
        except ClientError as e:
            logging.error(f"Error uploading to S3 - Bucket: {self.bucket_name}, Error: {str(e)}")
            raise 