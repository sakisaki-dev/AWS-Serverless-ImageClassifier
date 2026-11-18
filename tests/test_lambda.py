import pytest
import boto3
import os
import sys
import time
from botocore.exceptions import ClientError


"""
Test file that draws from my assets folder and prints it out nice and easy for me to evaluate parts of the program 
"""



sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from lambda_function import _extension

BUCKET_NAME = "image-classifier-processor"
REGION = "us-west-2"

class TestFinalSystemAudit:

    @classmethod
    def setup_class(cls):
        cls.s3 = boto3.client('s3', region_name=REGION)
        cls.rek = boto3.client('rekognition', region_name=REGION)
        cls.assets_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../assets'))
        
        print("\n🕒 Waiting for Lambda to finish processing (5s)...")
        time.sleep(5)

    def test_image_classification_results(self):
        """Detailed report of what Rekognition saw for each image."""
        print(f"\n\n{'='*25} IMAGE AUDIT {'='*25}")
        
        images = [f for f in os.listdir(self.assets_path) if _extension(f) in ['.jpg', '.jpeg', '.png']]
        
        for img in images:
            house_path = f"Houses/{img}"
            try:
                # 1. Check if it was moved to Houses
                self.s3.head_object(Bucket=BUCKET_NAME, Key=house_path)
                
                # 2. Get the labels Rekognition found to see WHY it was moved
                resp = self.rek.detect_labels(
                    Image={'S3Object': {'Bucket': BUCKET_NAME, 'Name': house_path}},
                    MaxLabels=3
                )
                labels = [f"{l['Name']} ({l['Confidence']:.1f}%)" for l in resp['Labels']]
                print(f"✅ [HOUSE]      {img.ljust(20)} -> Labels: {', '.join(labels)}")
            
            except ClientError:
                # If not in Houses, it was classified as Non-House
                print(f"❌ [NON-HOUSE]  {img.ljust(20)} -> Reason: No house labels > 70% confidence")

    def test_address_classification_results(self):
        """Detailed report of how your test.txt was split."""
        print(f"\n{'='*24} ADDRESS AUDIT {'='*24}")
        
        paths = {
            "USA": "Addresses/us_addresses.txt",
            "INTL": "Addresses/international_addresses.txt"
        }
        
        for category, key in paths.items():
            try:
                obj = self.s3.get_object(Bucket=BUCKET_NAME, Key=key)
                lines = obj['Body'].read().decode('utf-8').splitlines()
                
                print(f"\n--- {category} LIST ({len(lines)} addresses) ---")
                for line in lines: # Show all lines
                    print(f"  • {line}")
            except ClientError:
                print(f"\n⚠️  Missing output file: {key}")

        print(f"\n{'='*63}\n")
