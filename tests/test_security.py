"""Regresión de aislamiento y revocación de sesiones en base temporal."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DATABASE_URL'] = 'sqlite:///' + tempfile.mktemp(suffix='.db')
from core.db import get_engine, session_scope
from core.models import Base, Usuario, Proveedor
from core.auth import autenticar, alcance_proveedor, refrescar_usuario, hash_password

class SecurityTests(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(get_engine())
        Base.metadata.create_all(get_engine())
        with session_scope() as s:
            p = Proveedor(codigo='P1', nombre='P1', activo=True)
            s.add(p); s.flush()
            s.add(Usuario(email='p@test.com', nombre='P', rol='PROVEEDOR',
                          proveedor_id=p.id, password_hash=hash_password('valid-password'), activo=True))

    def test_missing_scope_denied(self):
        with self.assertRaises(PermissionError):
            alcance_proveedor({'rol': 'PROVEEDOR', 'proveedor_id': None})

    def test_provider_deactivation_revokes(self):
        user = autenticar('p@test.com', 'valid-password')
        self.assertIsNotNone(refrescar_usuario(user))
        with session_scope() as s:
            s.get(Proveedor, user['proveedor_id']).activo = False
        self.assertIsNone(refrescar_usuario(user))
        self.assertIsNone(autenticar('p@test.com', 'valid-password'))

    def test_role_change_revokes(self):
        user = autenticar('p@test.com', 'valid-password')
        with session_scope() as s:
            s.get(Usuario, user['id']).rol = 'INVENTARIOS'
        self.assertIsNone(refrescar_usuario(user))

    def test_user_deactivation_revokes(self):
        user = autenticar('p@test.com', 'valid-password')
        with session_scope() as s:
            s.get(Usuario, user['id']).activo = False
        self.assertIsNone(refrescar_usuario(user))

if __name__ == '__main__':
    unittest.main()
