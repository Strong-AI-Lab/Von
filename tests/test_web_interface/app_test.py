import unittest
from flask import Flask
from flask_testing import TestCase
from flask_login import login_user

# class AppTestCase(TestCase):
#     def create_app(self):
#         app.config['TESTING'] = True
#         app.config['SECRET_KEY'] = 'your_secret_key'
#         return app

#     def setUp(self):
#         self.client = app.test_client()

#     def test_home_redirects_to_login(self):
#         response = self.client.get('/', follow_redirects=False)
#         self.assertEqual(response.status_code, 302)
#         self.assertIn('/login', response.location)

#     def test_login_page_loads(self):
#         response = self.client.get('/login')
#         self.assert200(response)
#         self.assertIn(b'Login', response.data)

#     def test_valid_login(self):
#         response = self.client.post('/login', data=dict(
#             username='testuser',
#             password='testpass'
#         ), follow_redirects=True)
#         self.assert200(response)
#         self.assertIn(b'Welcome, testuser', response.data)

#     def test_invalid_login(self):
#         response = self.client.post('/login', data=dict(
#             username='wronguser',
#             password='wrongpass'
#         ), follow_redirects=True)
#         self.assert200(response)
#         self.assertIn(b'Invalid credentials', response.data)

#     def test_logout(self):
#         with self.client:
#             self.client.post('/login', data=dict(
#                 username='testuser',
#                 password='testpass'
#             ), follow_redirects=True)
#             response = self.client.get('/logout', follow_redirects=True)
#             self.assert200(response)
#             self.assertIn(b'You have been logged out', response.data)

# if __name__ == '__main__':
#     unittest.main()