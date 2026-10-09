import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from PIL import Image
from vk_api import VK, VKError

class PhotoTransportTests(unittest.TestCase):
    def test_bounded_copy_and_original_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'original.jpg'
            Image.new('RGB',(2560,1923)).save(path)
            before=path.read_bytes()
            client=VK('synthetic')
            client.call=Mock(side_effect=[{'upload_url':'https://pu.vk.com/upload'},[{'owner_id':1,'id':2}]])
            def upload(*args,**kwargs):
                picture=Image.open(kwargs['files']['photo'][1])
                self.assertLessEqual(max(picture.size),1280)
                return Mock(status_code=200,json=Mock(return_value={'photo':'data','server':1,'hash':'synthetic'}))
            with patch('vk_api.requests.post',side_effect=upload):
                self.assertEqual(client.photo(path),'photo1_2')
            self.assertEqual(path.read_bytes(),before)
    def test_gateway_failure_does_not_save_photo(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'photo.jpg';Image.new('RGB',(20,20)).save(path)
            client=VK('synthetic');client.call=Mock(return_value={'upload_url':'https://pu.vk.com/upload'})
            with patch('vk_api.requests.post',return_value=Mock(status_code=504)), patch('vk_api.time.sleep'):
                with self.assertRaisesRegex(VKError,'HTTP 504'):client.photo(path)
            self.assertEqual(client.call.call_count,4)

    def test_transient_failures_retry_with_fresh_url_before_save(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'photo.jpg';Image.new('RGB',(20,20)).save(path)
            client=VK('synthetic')
            client.call=Mock(side_effect=[{'upload_url':'https://pu.vk.com/a'}, {'upload_url':'https://pu.vk.com/b'}, {'upload_url':'https://pu.vk.com/c'}, [{'owner_id':1,'id':2}]])
            responses=[Mock(status_code=504),Mock(status_code=200,json=Mock(return_value={'photo':'[]','server':0,'hash':''})),Mock(status_code=200,json=Mock(return_value={'photo':'data','server':1,'hash':'synthetic'}))]
            with patch('vk_api.requests.post',side_effect=responses) as upload,patch('vk_api.time.sleep'):
                self.assertEqual(client.photo(path),'photo1_2')
            self.assertEqual([c.args[0] for c in upload.call_args_list],['https://pu.vk.com/a','https://pu.vk.com/b','https://pu.vk.com/c'])
            self.assertEqual([c.args[0] for c in client.call.call_args_list].count('photos.saveWallPhoto'),1)
