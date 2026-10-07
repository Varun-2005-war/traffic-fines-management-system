import os, re, tempfile, unittest
os.environ["TRAFFIC_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
import app as A


class Core(unittest.TestCase):
    def test_all_fines(self):
        for code, (_, amt) in A.RULES.items():
            self.assertEqual(A.calc_fine(code, 0), amt)
            self.assertEqual(A.calc_fine(code, 1), int(amt * 1.5))

    def test_bad_code(self):
        with self.assertRaises(ValueError):
            A.calc_fine(99, 0)

    def test_vehicle(self):
        self.assertEqual(A.normalize_vehicle("ap-31 ab 1234"), "AP31AB1234")
        self.assertTrue(A.valid_vehicle("AP31AB1234"))
        self.assertFalse(A.valid_vehicle("123"))


class Flow(unittest.TestCase):
    def setUp(self):
        A.app.config["TESTING"] = True
        self.c = A.app.test_client()

    def tok(self, path="/login"):
        return re.search(r'name=_csrf value="(\w+)"', self.c.get(path).get_data(as_text=True)).group(1)

    def login(self):
        self.c.post("/login", data={"username": "officer", "password": "officer123", "_csrf": self.tok()})

    def test_requires_login(self):
        self.assertEqual(self.c.get("/").status_code, 302)
        self.assertEqual(self.c.get("/lookup").status_code, 200)

    def test_issue_repeat_pay_idempotent(self):
        self.login(); t = self.tok("/new")
        d = {"vehicle_no": "ap 31 ab 1234", "owner_name": "Test", "violation_code": "1", "_csrf": t, "client_uuid": "u1"}
        r = self.c.post("/new", data=d); self.assertEqual(r.status_code, 302)
        r2 = self.c.post("/new", data=d); self.assertEqual(r.location, r2.location)  # idempotent
        d["client_uuid"] = "u2"; r3 = self.c.post("/new", data={**d, "fine_amount": "1"})
        page = self.c.get(r3.location).get_data(as_text=True)
        self.assertIn("₹1500", page)  # repeat escalation, tampered amount ignored
        no = r.location.rsplit("/", 1)[1]
        self.c.post(f"/pay/{no}", data={"_csrf": t})
        self.assertIn("PAID", self.c.get(f"/c/{no}").get_data(as_text=True))
        self.assertIn("AP31AB1234", self.c.get("/search?v=ap31ab1234").get_data(as_text=True))

    def test_csrf_and_validation(self):
        self.login()
        self.assertEqual(self.c.post("/new", data={"vehicle_no": "x"}).status_code, 400)
        r = self.c.post("/new", data={"vehicle_no": "bad", "owner_name": "a", "violation_code": "1", "_csrf": self.tok("/new")})
        self.assertEqual(r.status_code, 400)

    def test_audit_admin_only(self):
        self.login(); self.assertEqual(self.c.get("/audit").status_code, 403)


if __name__ == "__main__":
    unittest.main()
