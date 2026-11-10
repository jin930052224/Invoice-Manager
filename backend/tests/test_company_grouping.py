from sqlalchemy import create_engine, text, select
from datetime import date

from app.services.company_service import company_key_expression, choose_canonical_company_names
from app.models.invoice import Invoice


def test_company_identity_and_exact_filtering():
    engine=create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE invoices (id INTEGER, buyer_name TEXT, buyer_tax_id TEXT)'))
        db.execute(text('INSERT INTO invoices VALUES (:id,:name,:tax)'),[
            {'id':1,'name':'甲公司','tax':' abc123 '},
            {'id':2,'name':'甲公司名称误识别','tax':'ABC123'},
            {'id':3,'name':'甲公司','tax':'XYZ987'},
            {'id':4,'name':' 乙公司 ','tax':None},
            {'id':5,'name':'乙公司','tax':' '},
            {'id':6,'name':' ','tax':None},
            {'id':7,'name':None,'tax':None},
        ])
        key=company_key_expression()
        values=dict(db.execute(select(Invoice.id,key)).all())
        assert values[1]==values[2]=='tax:ABC123'
        assert values[1]!=values[3]
        assert values[4]==values[5]=='name:乙公司'
        assert values[6]==values[7]=='unclassified'
        assert db.execute(select(Invoice.id).where(key=='tax:ABC123')).scalars().all()==[1,2]
        assert db.execute(select(Invoice.id).where(key=='unclassified')).scalars().all()==[6,7]


def test_canonical_name_prefers_frequency_then_recency():
    rows = [
        {"key": "tax:A", "name": "杭州气浮科技有限公司", "occurrences": 2,
         "latest_issue_date": date(2026, 8, 31), "latest_id": 67},
        {"key": "tax:A", "name": "杭州气泽科技有限公司", "occurrences": 5,
         "latest_issue_date": date(2026, 9, 1), "latest_id": 70},
        {"key": "tax:B", "name": "旧名称", "occurrences": 1,
         "latest_issue_date": date(2026, 8, 27), "latest_id": 71},
        {"key": "tax:B", "name": "新名称", "occurrences": 1,
         "latest_issue_date": date(2026, 8, 28), "latest_id": 74},
    ]

    assert choose_canonical_company_names(rows) == {
        "tax:A": "杭州气泽科技有限公司",
        "tax:B": "新名称",
    }
