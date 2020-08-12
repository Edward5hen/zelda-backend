from flask import Flask, request
from flask_restful import Resource, Api, reqparse
from flask_pymongo import PyMongo
from flask_cors import CORS
import bson
from bson import json_util, ObjectId

from json import JSONEncoder
from collections import Counter


# Make ObjectID be JSON serializable
class CustomJSONEncoder(JSONEncoder):
    def default(self, obj):
        return json_util.default(obj)


class MyConfig(object):
    RESTFUL_JSON = {'cls': CustomJSONEncoder}


app = Flask(__name__)
mongo = PyMongo(app, uri="mongodb://localhost:27017/test")
app.config.from_object(MyConfig)
api = Api(app)
cors = CORS(app)
parser = reqparse.RequestParser()

def format_product_name(product_name):
    """Format product in URL into "Cap and Space".

    Args:
        product_name: product in URL -> "product-name" or "product_name"

    Returns:
        str: "Product Name"
    """
    _product = product_name.replace('-', ' ').replace('_', ' ')
    return ' '.join([i.capitalize() for i in _product.split()])


class RunSingle(Resource):
    """API: /zelda/runs/$run_name"""

    def put(self, run_name):
        run_data = request.get_json()
        run_data['run_name'] = run_name

        # Make sure product and cases can NOT be None or Empty.
        assert run_data['product']
        assert run_data['cases']

        test_cases = run_data['cases']
        for case in test_cases:
            case['_id'] = bson.objectid.ObjectId()

        # Format product name as "Capitalize And Space"
        product = run_data['product'] = format_product_name(run_data['product'])

        # run_name can't be duplicated, and should be short.
        if not mongo.db.runs.count_documents({}):
            mongo.db.runs.create_index([('run_name', 1)], unique=True)
        mongo.db.runs.insert_one(run_data)

        # Check if the products collection has this product in database and
        # Update this run to collection products.
        product_document = mongo.db.products.find_one({'name': product})
        if product_document is None:
            if not mongo.db.products.count_documents({}):
                mongo.db.products.create_index([('name', 1)], unique=True)
            mongo.db.products.insert_one({'name': product, 'runs': [run_name]})
        else:
            product_document['runs'].append(run_name)
            mongo.db.products.update_one({'name': product},
                {'$set': {'runs': product_document['runs']}})

        # Insert a document to runssum collection.
        run_sum = {}
        run_sum['run_name'] = run_name
        run_sum['product'] = product
        c = Counter(case['result'] for case in test_cases)
        run_sum['pass_count'] = c['0']                     # '0' represents 'Pass'
        run_sum['fail_count'] = c['1']                     # '1' represents 'Fail'
        run_sum['na_count'] = c['2']                       # '2' represents 'N/A'
        mongo.db.runssum.insert_one(run_sum)

        return {'run_name': run_name}

    def get(self, run_name):
        return mongo.db.runs.find_one({"run_name": run_name})

    def delete(self, run_name):
        # Remove the item in the list of the product.
        run_document = mongo.db.runs.find_one({'run_name': run_name})
        if run_document is None:
            raise Exception('%s is not found in database!' % run_name)

        product = run_document['product']
        product_document = mongo.db.products.find_one({'name': product})
        product_document['runs'].remove(run_name)
        mongo.db.products.update_one({'name': product},
            {'$set':{'runs': product_document['runs']}})

        # Remove the document if runs has no item
        if len(mongo.db.products.find_one({'name': product})['runs']) == 0:
            mongo.db.products.delete_one({'name': product})

        # Delete the record in collection runs.
        mongo.db.runs.delete_one({'run_name': run_name})

        # Delete the record in collection runssum
        mongo.db.runssum.delete_one({'run_name': run_name})


class Products(Resource):
    """API: /zelda/products"""

    def get(self):
        return list(mongo.db.products.find({}))


class ProductSingle(Resource):
    """API: /zelda/products/$product_name"""

    def get(self, product_name):
        """URL can NOT include space. Use minus or underline instead in URL.

        Args:
            product_name: will transfer to "Product Name" first

        Returns:
            Product respective document
        """
        return mongo.db.products.find_one({'name': format_product_name(product_name)})


class RunsSummaries(Resource):
    """API: /zelda/products/$product/runs/summaries"""

    def get(self, product_name):
        """URL can NOT include space. Use minus or underline instead in URL.

        Args:
            product_name: will transfer to "Product Name" first

        Returns:
            Runs summaries documents for this product.
        """
        return list(mongo.db.runssum.find({'product': format_product_name(product_name)}))


class Case(Resource):
    """API: /zelda/runs/$run_name/cases/$case_id"""

    def delete(self, run_name, case_id):
        # Modify result in runssum collection
        case_rst = ''
        correct_case_id_flag = False
        run = mongo.db.runs.find_one({'run_name': run_name})
        for case in run['cases']:
            if not case is None and str(case['_id']) == case_id:
                correct_case_id_flag = True
                case_rst = case['result']
        if not correct_case_id_flag:
            raise Exception('Not valid Case ID!')

        run_sum = mongo.db.runssum.find_one({'run_name': run_name})
        if case_rst == '0':
            run_sum['pass_count'] -= 1
        elif case_rst == '1':
            run_sum['fail_count'] -= 1
        elif case_rst == '2':
            run_sum['na_count'] -= 1
        mongo.db.runssum.update_one(
            {'run_name': run_name},
            {'$set':{
                'pass_count': run_sum['pass_count'],
                'fail_count': run_sum['fail_count'],
                'na_count': run_sum['na_count']
            }}
        )

        # Remove the record of respecitive run and respective case.
        mongo.db.runs.update_one(
            {'run_name': run_name},
            {'$unset':{'cases.$[case]': ''}},
            array_filters=[
                {'case._id': ObjectId(case_id)}
            ]
        )


class CaseUpdate(Resource):
    """API: /zelda/runs/$run_name/cases/$case_id/update"""

    def post(self, run_name, case_id):
        """post method to update test case.

        Data Arguments:
            bug: bugzilla ID
            comments: case result comment

        cURL example:
        curl -X POST http://$IP:$PORT/zelda/runs/$run_name/cases/$case_id/update
         -d 'bug=12345' -d 'comments=test comments'
        """
        parser.add_argument('bug', type=str, required=True)
        parser.add_argument('comments', type=str, required=True)
        args = parser.parse_args()
        mongo.db.runs.update_one(
            {'run_name': run_name},
            {'$set':{
                'cases.$[case].bug': args['bug'],
                'cases.$[case].comments': args['comments']
            }},
            array_filters=[
                {'case._id': ObjectId(case_id)}
            ],
            upsert=False
        )


api.add_resource(RunSingle, '/zelda/runs/<string:run_name>')
api.add_resource(Products, '/zelda/products')
api.add_resource(ProductSingle, '/zelda/products/<string:product_name>')
api.add_resource(RunsSummaries, '/zelda/products/<string:product_name>/runs/summaries')
api.add_resource(Case, '/zelda/runs/<string:run_name>/cases/<string:case_id>')
api.add_resource(CaseUpdate, '/zelda/runs/<string:run_name>/cases/<string:case_id>/update')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port='12321', debug=True)