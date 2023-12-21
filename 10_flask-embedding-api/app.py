
from flask import Flask, request
from instructor_model import InstructorModel
import sqlite_helpers
from typing import List
import logging
import numpy as np
import json
from wsgiref.simple_server import make_server
from config import database_path, instructor_model_name
import heapq
import random
import hnswlib
import time

instructor_model = None
description_index = None
review_index = None

app = Flask(__name__)

# Startup code
with app.app_context():
    logging.basicConfig(level=logging.INFO)
    print('Loading instructor model...')
    instructor_model = InstructorModel(instructor_model_name)

    print('Loading indexes...')
    conn = sqlite_helpers.create_connection(database_path)
    if sqlite_helpers.database_has_indexes_available(conn):
        description_index = sqlite_helpers.load_latest_description_index(conn)
        review_index = sqlite_helpers.load_latest_review_index(conn)
    else:
        print("No indexes found, exiting...")
        exit(1)
    conn.close()


@app.route('/get_results')
def get_results():
    # Automatically route to the correct function
    # If the query is numeric, assume it's an appid
    # Otherwise, assume it's a query string
    query = request.args.get('query')
    if query is None:
        return 'No query specified', 400
    
    try:
        query = int(query)
        return get_similar_games()
    except ValueError:
        return get_query_results()

@app.route('/get_query_results')
def get_query_results():
    global instructor_model

    query = request.args.get('query')

    type = request.args.get('type')
    type = 'all' if type is None else type
    known_types = ['all', 'description', 'review']
    if type not in known_types:
        return 'Invalid type, must be one of: {}'.format(', '.join(known_types)), 400

    instruction = request.args.get('instruction')
    instruction = 'Represent a video game that has a description of:' if instruction is None else instruction

    num_results = request.args.get('num_results')
    num_results = 10 if num_results is None else int(num_results)
    num_results = max(0, min(num_results, 100))

    logging.info(f'Request: {request.url}')

    # Generate query embedding
    query_tokenized = instructor_model.tokenize(query)
    instructor_model.embedding_instruction = instruction

    if len(query_tokenized) > instructor_model.get_max_query_chunk_length():
        return 'Query too long, shorten query or instruction', 400
    
    query_embed = instructor_model.generate_embedding_for_query(query)

    # Query for results
    conn = sqlite_helpers.create_connection(database_path)
    search_time_begin = time.perf_counter()
    #results = search(conn, query_embed, type, max_results=num_results)
    results = index_search(conn, query_embed, type, max_results=num_results)
    search_time_end = time.perf_counter()
    logging.info(f"Search time: {search_time_end - search_time_begin}")
    conn.close()

    # Return results as JSON
    response = app.response_class(
        response=json.dumps(results),
        status=200,
        mimetype='application/json'
    )

    # TODO: Limit this to the domain of the frontend
    response.headers['Access-Control-Allow-Origin'] = '*'

    return response


@app.route('/get_similar_games')
def get_similar_games():
    global instructor_model

    appid = int(request.args.get('query'))
    num_results = request.args.get('num_results')
    num_results = 10 if num_results is None else int(num_results)
    num_results = max(0, min(num_results, 100))

    logging.info(f'Request: {request.url}')

    # Query for results
    conn = sqlite_helpers.create_connection(database_path)
    
    #results = search_similar(conn, appid, 'all', max_results=num_results)
    search_time_begin = time.perf_counter()
    results = index_search_similar(conn, appid, 'all', max_results=num_results)
    search_time_end = time.perf_counter()
    logging.info(f"Search time: {search_time_end - search_time_begin}")

    conn.close()

    # Return results as JSON
    response = app.response_class(
        response=json.dumps(results),
        status=200,
        mimetype='application/json'
    )

    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


def cosine_similarity(a: List[float], b: List[float]) -> float:
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))

def mean_pooling(embeddings: List[List[float]]) -> List[float]:
    return np.sum(embeddings, axis=0) / len(embeddings)

def compare_all_embeddings_take_max(embeddings: List[List[float]], query_embed: List[float]) -> float:
    similarities = [cosine_similarity(embedding, query_embed) for embedding in embeddings]
    return max(similarities)

def add_to_heap(heap: List[dict], item_to_add: dict, max_length: int):
    # Add to heap
    # Add a random number to the tuple to break ties, since you can't compare dicts
    heapq.heappush(heap, (item_to_add['score'], random.random() , item_to_add))

    # Pop if too large
    if len(heap) > max_length:
        heapq.heappop(heap)


def search(conn, query_embed, query_for_type, max_results=10):
    matches = []
    logging.debug(f"Searching for {query_for_type} matches")

    # Store Description Search
    if query_for_type == 'all' or query_for_type == 'description':
        for current_page in sqlite_helpers.get_paginated_embeddings_for_descriptions(conn, page_size=100):
            for appid, embeddings in current_page.items():
                score = compare_all_embeddings_take_max(embeddings, query_embed)
                name = sqlite_helpers.get_name_for_appid(conn, appid)

                add_to_heap(matches, {
                    'appid': appid,
                    'name': name,
                    'match_type': 'description',
                    'score': float(score),
                }, max_results)
    
    # Review Search
    if query_for_type == 'all' or query_for_type == 'review':
        appids_with_reviews = sqlite_helpers.get_appids_with_review_embeds(conn)

        for appid in appids_with_reviews:
            all_review_embeddings = sqlite_helpers.get_review_embeddings_for_appid(conn, appid)
            flat_embeddings = [review_embedding for review_id in all_review_embeddings for review_embedding in all_review_embeddings[review_id]]
            average_embedding = mean_pooling(flat_embeddings)

            score = cosine_similarity(average_embedding, query_embed)
            name = sqlite_helpers.get_name_for_appid(conn, appid)

            add_to_heap(matches, {
                'appid': appid,
                'name': name,
                'match_type': 'review',
                'score': float(score),
            }, max_results)
    
    # Sort by score (first element of tuple)
    matches.sort(key=lambda x: x[0], reverse=True)

    # Remove scores and random numbers
    matches = [match[2] for match in matches]

    return matches

def index_search(conn, query, query_for_type, max_results=10):
    matches = []
    logging.debug(f"Searching for {query_for_type} matches")

    # Store Description Search
    if query_for_type == 'all' or query_for_type == 'description':
        #description_index = sqlite_helpers.load_latest_description_index(conn)
        global description_index
        appids, distances = description_index.knn_query(query, k=max_results)
        
        appids = appids[0]
        distances = distances[0]

        for appid, distance in zip(appids, distances):
            appid = int(appid)
            distance = float(distance)

            name = sqlite_helpers.get_name_for_appid(conn, appid)
            matches.append({
                'appid': appid,
                'name': name,
                'match_type': 'description',
                'score': 1.0 - distance,
            })

    # Review search - Calculate average embedding for all reviews / mean pooling
    if query_for_type == 'all' or query_for_type == 'review':
        #review_index = sqlite_helpers.load_latest_review_index(conn)
        global review_index
        appids, distances = review_index.knn_query(query, k=max_results)

        appids = appids[0]
        distances = distances[0]

        for appid, distance in zip(appids, distances):
            appid = int(appid)
            distance = float(distance)

            name = sqlite_helpers.get_name_for_appid(conn, appid)
            matches.append({
                'appid': appid,
                'name': name,
                'match_type': 'review',
                'score': 1.0 - distance,
            })

    # Order by score
    matches = sorted(matches, key=lambda x: x['score'], reverse=True)

    return matches[:max_results]

def search_similar(conn, query_appid, query_for_type, max_results=10):
    matches = []
    logging.debug(f"Searching for similar games to {sqlite_helpers.get_name_for_appid(conn, query_appid)}")

    # Store Description Search
    if query_for_type == 'all' or query_for_type == 'description':
        all_description_embeddings = sqlite_helpers.get_description_embeddings_for_appid(conn, query_appid)
        query_embed = mean_pooling(all_description_embeddings)

        for current_page in sqlite_helpers.get_paginated_embeddings_for_descriptions(conn, page_size=100):
            for current_appid, embeddings in current_page.items():
                if current_appid == query_appid:
                    continue

                score = compare_all_embeddings_take_max(embeddings, query_embed)
                name = sqlite_helpers.get_name_for_appid(conn, current_appid)

                add_to_heap(matches, {
                    'appid': current_appid,
                    'name': name,
                    'match_type': 'description',
                    'score': float(score),
                }, max_results)

    # Review search v2 - Calculate average embedding for all reviews / mean pooling
    if query_for_type == 'all' or query_for_type == 'review':
        query_review_embeddings = sqlite_helpers.get_review_embeddings_for_appid(conn, query_appid)
        query_flat_embeddings = [review_embedding for review_id in query_review_embeddings for review_embedding in query_review_embeddings[review_id]]
        query_embed = mean_pooling(query_flat_embeddings)

        appids_with_reviews = sqlite_helpers.get_appids_with_review_embeds(conn)

        for current_appid in appids_with_reviews:
            if current_appid == query_appid:
                continue
            
            all_review_embeddings = sqlite_helpers.get_review_embeddings_for_appid(conn, current_appid)
            flat_embeddings = [review_embedding for review_id in all_review_embeddings for review_embedding in all_review_embeddings[review_id]]
            average_embedding = mean_pooling(flat_embeddings)

            score = cosine_similarity(average_embedding, query_embed)
            #score = euclidean_distance(average_embedding, query_embed)
            name = sqlite_helpers.get_name_for_appid(conn, current_appid)

            add_to_heap(matches, {
                'appid': current_appid,
                'name': name,
                'match_type': 'review',
                'score': float(score),
            }, max_results)

    # Sort by score (first element of tuple)
    matches.sort(key=lambda x: x[0], reverse=True)

    # Remove scores and random numbers
    matches = [match[2] for match in matches]

    return matches

def index_search_similar(conn, query_appid, query_for_type, max_results=10):
    matches = []
    logging.debug(f"Searching for similar games to {sqlite_helpers.get_name_for_appid(conn, query_appid)}")

    # Store Description Search
    if query_for_type == 'all' or query_for_type == 'description':
        #description_index = sqlite_helpers.load_latest_description_index(conn)
        global description_index
        all_description_embeddings = sqlite_helpers.get_description_embeddings_for_appid(conn, query_appid)
        query_embed = mean_pooling(all_description_embeddings)

        # Add 1 to max results to account for the query returning
        # the app we're searching for
        appids, distances = description_index.knn_query(query_embed, k=max_results + 1)
        
        appids = appids[0]
        distances = distances[0]

        for appid, distance in zip(appids, distances):
            appid = int(appid)
            distance = float(distance)

            if appid == query_appid:
                continue

            name = sqlite_helpers.get_name_for_appid(conn, appid)
            matches.append({
                'appid': appid,
                'name': name,
                'match_type': 'description',
                'score': 1.0 - distance,
            })

    # Review search - Calculate average embedding for all reviews / mean pooling
    if query_for_type == 'all' or query_for_type == 'review':
        #review_index = sqlite_helpers.load_latest_review_index(conn)
        global review_index
        
        query_review_embeddings = sqlite_helpers.get_review_embeddings_for_appid(conn, query_appid)
        logging.info(f"Basing review query on {len(query_review_embeddings)} user reviews.")
        query_flat_embeddings = [review_embedding for review_id in query_review_embeddings for review_embedding in query_review_embeddings[review_id]]
        query_embed = mean_pooling(query_flat_embeddings)

        appids, distances = review_index.knn_query(query_embed, k=max_results + 1)

        appids = appids[0]
        distances = distances[0]

        for appid, distance in zip(appids, distances):
            appid = int(appid)
            distance = float(distance)

            if appid == query_appid:
                continue

            name = sqlite_helpers.get_name_for_appid(conn, appid)
            matches.append({
                'appid': appid,
                'name': name,
                'match_type': 'review',
                'score': 1.0 - distance,
            })

    # Order by score
    matches = sorted(matches, key=lambda x: x['score'], reverse=True)

    return matches[:max_results]

if __name__ == '__main__':
    server = make_server('localhost', 5001, app)
    server.serve_forever()
