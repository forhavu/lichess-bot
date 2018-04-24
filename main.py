import argparse
import chess
from chess.variant import find_variant
import chess.polyglot
import engine_wrapper
import model
import json
import lichess
import logging
import multiprocessing
import traceback
import logging_pool
from config import load_config
from conversation import Conversation, ChatLine
from functools import partial
from requests.exceptions import ConnectionError, HTTPError
from urllib3.exceptions import ProtocolError

try:
    from http.client import RemoteDisconnected
    # New in version 3.5: Previously, BadStatusLine('') was raised.
except ImportError:
    from http.client import BadStatusLine as RemoteDisconnected

__version__ = "0.9"

def upgrade_account(li):
    if li.upgrade_to_bot_account() is None:
        return False

    print("Succesfully upgraded to Bot Account!")
    return True

def watch_control_stream(control_queue, li):
    for evnt in li.get_event_stream().iter_lines():
        if evnt:
            event = json.loads(evnt.decode('utf-8'))
            control_queue.put_nowait(event)
        else:
            control_queue.put_nowait({"type": "ping"})

def start(li, user_profile, engine_factory, config):
    # init
    max_games = config["max_concurrent_games"]
    print("You're now connected to {} and awaiting challenges.".format(config["url"]))
    manager = multiprocessing.Manager()
    challenge_queue = []
    control_queue = manager.Queue()
    control_stream = multiprocessing.Process(target=watch_control_stream, args=[control_queue, li])
    control_stream.start()
    busy_processes = 0
    queued_processes = 0

    with logging_pool.LoggingPool(max_games+1) as pool:
        while True:
            event = control_queue.get()
            if event["type"] == "local_game_done":
                busy_processes -= 1
                print("+++ Process Free. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            elif event["type"] == "challenge":
                chlng = model.Challenge(event["challenge"])
                if chlng.is_supported(config):
                    challenge_queue.append(chlng)
                    if (config.get("sort_challenges_by") != "first"):
                        challenge_queue.sort(key=lambda c: -c.score())
                else:
                    try:
                        li.decline_challenge(chlng.id)
                        print("    Decline {}".format(chlng))
                    except HTTPError as exception:
                        if exception.response.status_code != 404: # ignore missing challenge
                            raise exception
            elif event["type"] == "gameStart":
                if queued_processes <= 0:
                    print("Something went wrong. Game is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                game_id = event["game"]["id"]
                pool.apply_async(play_game, [li, game_id, control_queue, engine_factory, user_profile, config])
                busy_processes += 1
                print("--- Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))

            while ((queued_processes + busy_processes) < max_games and challenge_queue): # keep processing the queue until empty or max_games is reached
                chlng = challenge_queue.pop(0)
                try:
                    response = li.accept_challenge(chlng.id)
                    print("    Accept {}".format(chlng))
                    queued_processes += 1
                    print("--- Process Queue. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                except HTTPError as exception:
                    if exception.response.status_code == 404: # ignore missing challenge
                        print("    Skip missing {}".format(chlng))
                    else:
                        raise exception

    control_stream.terminate()
    control_stream.join()

def play_game(li, game_id, control_queue, engine_factory, user_profile, config):
    updates = li.get_game_stream(game_id).iter_lines()

    #Initial response of stream will be the full game info. Store it
    game = model.Game(json.loads(next(updates).decode('utf-8')), user_profile["username"], li.baseUrl)
    board = setup_board(game)
    engine = engine_factory(board)
    conversation = Conversation(game, engine, li, __version__)

    print("+++ {}".format(game))

    engine.pre_game(game)

    engine_cfg = config["engine"]

    if (engine_cfg["polyglot"] == True):
        board = play_first_book_move(game, engine, board, li, engine_cfg)
    else:
        board = play_first_move(game, engine, board, li)

    try:
        for binary_chunk in updates:
            upd = json.loads(binary_chunk.decode('utf-8')) if binary_chunk else None
            u_type = upd["type"] if upd else "ping"
            if u_type == "chatLine":
                conversation.react(ChatLine(upd), game)
            elif u_type == "gameState":
                game.state = upd
                moves = upd["moves"].split()
                board = update_board(board, moves[-1])
                if is_engine_move(game, moves):
                    best_move = None
                    if (engine_cfg["polyglot"] == True and len(moves) <= (engine_cfg["polyglot_max_depth"] * 2) - 1):
                        best_move = get_book_move(board, engine_cfg)
                    if best_move == None:
                        best_move = engine.search(board, upd["wtime"], upd["btime"], upd["winc"], upd["binc"])
                    li.make_move(game.id, best_move)
                    game.abort_in(20)
            elif u_type == "ping":
                if game.should_abort_now() and len(game.state["moves"]) < 6:
                    print("    Aborting {} by lack of activity".format(game.url()))
                    li.abort(game.id)
    except (RemoteDisconnected, ConnectionError, ProtocolError, HTTPError) as exception:
        print("Abandoning game due to connection error")
        traceback.print_exception(type(exception), exception, exception.__traceback__)
    finally:
        print("--- {} Game over".format(game.url()))
        engine.quit()
        # This can raise queue.NoFull, but that should only happen if we're not processing
        # events fast enough and in this case I believe the exception should be raised
        control_queue.put_nowait({"type": "local_game_done"})


def can_accept_challenge(chlng, config):
    return chlng.is_supported(config)

def play_first_move(game, engine, board, li):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        # need to hardcode first movetime since Lichess has 30 sec limit.
        best_move = engine.first_search(board, 2000)
        li.make_move(game.id, best_move)

    return board


def play_first_book_move(game, engine, board, li, config):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        book_move = get_book_move(board, config)
        if (book_move != None):
            li.make_move(game.id, book_move)
        else:
            return play_first_move(game, engine, board, li)

    return board


def get_book_move(board, engine_cfg):
    try:
        with chess.polyglot.open_reader(engine_cfg["polyglot_book"]) as reader:
            if (engine_cfg["polyglot_random"] == True):
                book_move = reader.choice(board).move()
            else:
                book_move = reader.find(board, engine_cfg["polyglot_min_weight"]).move()
            return book_move
    except:
        pass

    return None


def setup_board(game):
    if game.variant_name.lower() == "chess960":
        board = chess.Board(game.initial_fen, chess960=True)
    elif game.variant_name == "From Position":
        board = chess.Board(game.initial_fen)
    else:
        VariantBoard = find_variant(game.variant_name)
        board = VariantBoard()
    moves = game.state["moves"].split()
    for move in moves:
        board = update_board(board, move)

    return board


def is_white_to_move(game, moves):
    return len(moves) % 2 == (0 if game.white_starts else 1)


def is_engine_move(game, moves):
    is_w = (game.is_white and is_white_to_move(game, moves))
    is_b = (game.is_white is False and is_white_to_move(game, moves) is False)

    return (is_w or is_b)


def update_board(board, move):
    uci_move = chess.Move.from_uci(move)
    board.push(uci_move)
    return board

def intro():
    return r"""
.   _/|
.  // o\
.  || ._)  lichess-bot %s
.  //__\
.  )___(   Play on Lichess with a bot
""".lstrip() % __version__

if __name__ == "__main__":
    print(intro())
    logger = logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description='Play on Lichess with a bot')
    parser.add_argument('-u', action='store_true', help='Add this flag to upgrade your account to a bot account.')
    args = parser.parse_args()
    CONFIG = load_config()
    li = lichess.Lichess(CONFIG["token"], CONFIG["url"], __version__)

    user_profile = li.get_profile()
    username = user_profile["username"]
    is_bot = user_profile.get("title") == "BOT"
    print("Welcome {}!".format(username))

    if args.u is True and is_bot is False:
        is_bot = upgrade_account(li)

    if is_bot:
        engine_factory = partial(engine_wrapper.create_engine, CONFIG)
        start(li, user_profile, engine_factory, CONFIG)
    else:
        print("{} is not a bot account. Please upgrade your it to a bot account!".format(user_profile["username"]))
