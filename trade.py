import json
import math
import requests
import os.path
from pathlib import Path
import heapq
from bs4 import BeautifulSoup
import hashlib
from urllib.parse import urlparse, parse_qs
import pulp

AVERAGE_D6 = 3.5

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'


class Deal:
    def __init__(self, trade_good, tons, purchase_price, sale_price, sort_value) -> None:
        self.trade_good = trade_good
        self.tons = tons
        self.purchase_price = purchase_price
        self.sale_price = sale_price
        self.sort_value = sort_value


class TradeGood:
    def __init__(self, data, data_loader) -> None:
        self.name = data["name"]
        self.__availability = set(data["availability"]) if data["availability"] != "All" else None
        self.__tons_dice = data["tonsDice"]
        self.__tons_multiplier = data["tonsMultiplier"]
        self.__base_price = data["basePrice"]
        self.__purchase_modifier = data["purchaseModifier"]
        self.__sale_modifier = data["saleModifier"]
        self.__max_law_level = data["maxLawLevel"]
        self.data_loader = data_loader

    def tons_available(self, world, starting_planet):
        if not self.is_available(world, starting_planet):
            return 0
        
        if starting_planet and world.has_snapshot():
            snapshot = world.get_purchase_snapshot(self.name)
            return snapshot["tons"]

        modifier = 0

        if world.population <= 3:
            modifier = -3
        elif world.population >= 9:
            modifier = 3

        dice_roll = (self.__tons_dice * AVERAGE_D6) + modifier
        return dice_roll * self.__tons_multiplier
    
    def is_available(self, world, starting_planet):
        if starting_planet and world.has_snapshot():
            snapshot = world.get_purchase_snapshot(self.name)
            return snapshot is not None and snapshot["tons"] != 0

        if world.size is None:
            return False

        if self.__availability is None:
            return True
        
        trade_codes = set(world.remarks)

        if trade_codes.intersection(self.__availability):
            return True
        
        return False
    
    def is_illegal(self, world):
        if self.__max_law_level is None:
            return False
        
        return self.__max_law_level <= world.law
    
    def purchase_price(self, skill, world, starting_planet):
        if starting_planet and world.has_snapshot():
            snapshot = world.get_purchase_snapshot(self.name)
            return snapshot["currentPrice"]
        
        return self.__best_price(world, skill, "purchase")

    
    def sale_price(self, skill, world, starting_planet):
        if starting_planet and world.has_snapshot():
            snapshot = world.get_sale_snapshot(self.name)
            return snapshot["currentPrice"]

        return self.__best_price(world, skill, "sale")
    

    def __best_price(self, world, skill, type):
        best_modifier = None
        modifiers = self.__purchase_modifier if type == "purchase" else self.__sale_modifier

        for remark in world.remarks:
            if remark in modifiers:
                modifier = modifiers[remark]

                if best_modifier is None or best_modifier < modifier:
                    best_modifier = modifier

        if best_modifier is None:
            best_modifier = 0

        roll = best_modifier + skill + (3 * AVERAGE_D6)

        lower = self.data_loader.modified_price(math.floor(roll), type)
        upper = self.data_loader.modified_price(math.ceil(roll), type)

        factor = (lower + upper) / 2
        return factor * self.__base_price / 100

class World:
    def __init__(self, data, data_loader) -> None:
        uwp = data["UWP"]
        self.starport = uwp[0]
        self.size = self.__parse_hex(uwp[1])
        self.atmosphere = self.__parse_hex(uwp[2])
        self.hydrographics = self.__parse_hex(uwp[3])
        self.population = self.__parse_hex(uwp[4])
        self.government = self.__parse_hex(uwp[5])
        self.law = self.__parse_hex(uwp[6])
        self.tech = self.__parse_hex(uwp[8])
        self.sector_hex = SectorHex(data["Sector"], data["Hex"])
        self.name = data["Name"]
        self.x = int(data["WorldX"])
        self.y = int(data["WorldY"])
        self.zone = data["Zone"]
        self.data_loader = data_loader
        self.__neighbours = None
        self.allegiance = data["Allegiance"]
        self.__trade_snapshot = None

        self.remarks = data["Remarks"].split()

    @staticmethod
    def __parse_hex(hex):
        if hex == "?":
            return None
        
        return int(hex, 18)

    def __eq__(self, other):
        return self.sector_hex == other.sector_hex
    
    def __hash__(self) -> int:
        return hash(self.sector_hex)
    
    def __str__(self) -> str:
        return self.sector_hex.__str__()
    
    def __repr__(self) -> str:
        return self.sector_hex.__repr__()
    
    def set_trade_snapshot(self, snapshot):
        self.__trade_snapshot = snapshot

    def get_sale_snapshot(self, good):
        if self.__trade_snapshot is None:
            return None

        for item in self.__trade_snapshot["desiredGoods"]:
            if item["type"] == good:
                return item
        
        return None
    
    def freight_snapshot(self, other_world, cargo):
        if not self.has_snapshot():
            return None, None
        
        problem = pulp.LpProblem('Freight', pulp.LpMaximize)
        total = 0
        vars = []

        freight = self.__trade_snapshot["planets"][other_world.name]["freight"]
        i = 0

        for item in freight:
            i+= 1
            variable = pulp.LpVariable(f"x{i}", cat="Binary")
            total += item["tons"] * variable
            vars.append(variable)

        problem += total <= cargo
        problem += total
        problem.solve(pulp.PULP_CBC_CMD(msg=False))

        freight = [freight[i] for i, variable in enumerate(vars) if pulp.value(variable) == 1]
        freight = [f"{item['tons']}x {item["contents"]}" for item in freight]
        return pulp.value(total), ",".join(freight)
    
    def has_snapshot(self):
        return self.__trade_snapshot is not None

    def get_purchase_snapshot(self, good):
        if self.__trade_snapshot is None:
            return None
        
        for item in self.__trade_snapshot["availableTradeGoods"]:
            if item["type"] == good:
                return item
        
        return None

    @property
    def neighbours(self):
        if self.__neighbours is None:
            self.data_loader.load_world_data(self.sector_hex, True)

        return self.__neighbours
    
    @neighbours.setter
    def neighbours(self, neighbours):
        self.__neighbours = neighbours

    def __passenger_count(self, level, ship, other_world, starting_world):
        distance = self.distance(other_world)

        if starting_world and self.has_snapshot():
            return self.__trade_snapshot["planets"][other_world.name]["passengers"][level]

        modifier = ship.max_steward

        if distance > 1:
            modifier -= distance - 1

        if level == "high":
            modifier -= 4
        elif level == "low":
            modifier += 1

        if self.population <= 1:
            modifier -= 4
        elif self.population in [6,7]:
            modifier += 1
        elif self.population >= 8:
            modifier += 3
        
        match self.starport:
            case "A":
                modifier += 2
            case "B":
                modifier += 2
            case "E":
                modifier -= 1
            case "X":
                modifier -= 3

        match self.zone:
            case "R":
                modifier -= 4
            case "A":
                modifier += 1

        roll = modifier + 2 * AVERAGE_D6

        cold_war = (
            (self.sector_hex in NEU_BAYERN and other_world.sector_hex in AMONDIAGE)
            or
            (self.sector_hex in AMONDIAGE and other_world.sector_hex in NEU_BAYERN)
        )

        if cold_war:
            modifier -= 2

        upper = self.data_loader.passenger_count(math.ceil(roll))
        lower = self.data_loader.passenger_count(math.floor(roll))

        return (upper + lower) /2

        
    def distance(self, other_world):
        x1 = self.x
        y1 = self.y
        x2 = other_world.x
        y2 = other_world.y
        return round((((x1 - x2) ** 2) + ((y1-y2) ** 2)) ** (1/2))
    
    def passengers(self, other_world, ship, starting_world):
        distance = self.distance(other_world)
        passenger_revenue = 0
        passage_descriptions = []

        for passage in ship.passage:
            ticket_price = self.data_loader.passage(passage.type, distance)
            passengers = min(self.__passenger_count(passage.type, ship, other_world, starting_world), passage.number)
            life_support = self.data_loader.life_support(passage.type) * distance / 4
            passenger_revenue += passengers * (ticket_price - life_support)
            passage_descriptions.append(f"{passengers} {passage.type} at {ticket_price} with life support of {life_support}")

            if passage.type == "middle" and passengers < passage.number:
                passengers = min(self.__passenger_count("basic", ship, other_world, starting_world), (passage.number - passengers) * 2)
                ticket_price = self.data_loader.passage("basic", distance)
                passenger_revenue += passengers * ticket_price
                passage_descriptions.append(f"{passengers} basic at {ticket_price}")

        return passenger_revenue, f"Took on passengers: {", ".join(passage_descriptions)}"


    def best_trades(self, other_world, trade_goods, ship, capital, starting_planet):
        distance = self.distance(other_world)
        cargo = ship.cargo_capacity(distance)
        freight_per_ton = self.data_loader.passage("freight", distance)
        
        if cargo is None:
            return None, None, None

        deals = []

        not_available = []
        unaffordable = []
        no_profit = []
        illegal = []

        for trade_good in trade_goods:
            if not trade_good.is_available(self, starting_planet):
                not_available.append(trade_good.name)
                continue

            if trade_good.is_illegal(self) or trade_good.is_illegal(other_world):
                illegal.append(trade_good.name)
                continue

            purchase_price = trade_good.purchase_price(ship.max_broker, self, starting_planet)

            if purchase_price > capital:
                unaffordable.append(trade_good.name)
                continue

            sale_price = trade_good.sale_price(ship.max_broker, other_world, False)

            if sale_price - purchase_price < freight_per_ton:
                no_profit.append(f"{trade_good.name} ({sale_price} - {purchase_price})")
                continue

            available_tons = trade_good.tons_available(self, starting_planet)

            available_tons = min(cargo, available_tons)
            available_tons = min(capital/ purchase_price, available_tons)

            sort_value = available_tons * (sale_price - purchase_price) + ((cargo - available_tons) * freight_per_ton)
            actual_tons = math.floor(available_tons)

            deals.append(Deal(trade_good.name, actual_tons, purchase_price, sale_price, sort_value))

        deals = sorted(deals, key=lambda x: x.sort_value, reverse=True)

        starting_capital = capital
        final_capital = capital

        executed_deals = []

        for deal in deals:
            if capital < deal.purchase_price:
                continue

            amount = min(math.floor(capital / deal.purchase_price), deal.tons)
            amount = min(amount, cargo)
            profit = amount * (deal.sale_price - deal.purchase_price)
            if amount != 0:
                executed_deals.append(f"Buy {amount} of {deal.trade_good} at {deal.purchase_price}, sell at {deal.sale_price}, total profit: {profit:,.2f}, capital: {final_capital:,.2f}->{final_capital + profit:,.2f}")
                final_capital += profit
                cargo -= amount
                capital -= amount * deal.purchase_price
        
        if cargo > 0:
            freight_revenue = 0

            if starting_planet and self.has_snapshot():
                tons, text = self.freight_snapshot(other_world, cargo)
                executed_deals.append(f"Carrying the following Freight: {text}")
                executed_deals.append(f"Remaining cargo is {cargo - tons}")
                cargo = tons

            freight_revenue = cargo * freight_per_ton
            executed_deals.append(f"Do {cargo} tons of freight for {freight_revenue} capital: {final_capital:,.2f}->{final_capital + freight_revenue:,.2f}")
            final_capital += freight_revenue
        else:
            executed_deals.append("Cargo is full, no freight")
        
        executed_deals.append(f"Cash after goods are purchased is {capital:,.2f}")

        return starting_capital, final_capital, executed_deals

MORTGAGE_PAID = "mortgage_paid"

class Mortgage:
    def __init__(self, mortgage, monthly_payment=None):
        self.__mortgage = mortgage

        if monthly_payment is None:
            self.__monthly_payment = mortgage / 240
        else:
            self.__monthly_payment = monthly_payment

    def mortgage_payment(self, state):
        paid = state.get(MORTGAGE_PAID, 0)
        payment = self.__monthly_payment

        if self.__mortgage - paid < payment:
            payment = self.__mortgage - paid
        
        state[MORTGAGE_PAID] = paid + payment
        return payment
    
    def profit_cut(self, *argv):
        return None, None
    
    def monthly_income(self):
        return 0
    
    def current_cut(self, _):
        return 0

class Ship:
    def __init__(self, monthly_maint, fuel_per_jump, max_jump, fuel_tank, cargo, cargo_fuel, passage, contract, max_steward, max_broker, banned_allegiances =[]) -> None:
        self.monthly_maint = monthly_maint
        self.__fuel_per_jump = fuel_per_jump
        self.__max_jump = max_jump
        self.__fuel_tank = fuel_tank
        self.__cargo = cargo
        self.__cargo_fuel = cargo_fuel
        self.passage = passage
        self.contract = contract
        self.max_steward = max_steward
        self.max_broker = max_broker
        self.banned_allegiances = banned_allegiances

    def monthly_life_support(self, data_loader):
        life_support = 0

        for passage in self.passage:
            if passage.type == "low":
                continue
            life_support += data_loader.life_support(passage.type) * passage.number

        return life_support

    def cargo_capacity(self, distance):
        fuel_required = distance * self.__fuel_per_jump
        fuel_required = max(fuel_required - self.__fuel_tank, 0)
        
        if self.__cargo_fuel < fuel_required:
            return None
        
        return self.__cargo + self.__cargo_fuel - fuel_required
    
    def max_jump(self):
        return math.floor((self.__cargo_fuel + self.__fuel_tank) / self.__fuel_per_jump)

    def jumps_required(self, distance):
        return math.ceil(distance / self.__max_jump)
    
    def expected_duration(self, distance):
        max_jump = self.max_jump()
        jumps = self.jumps_required(distance)
        return math.ceil(distance / max_jump) + jumps

    def fuel_cost(self, distance):
        # Assumes fuel is unprocessed
        return distance * self.__fuel_per_jump * 100
    
class SectorHex:
    def __init__(self, sector, hex) -> None:
        self.hex = hex
        self.sector = sector.lower()
        self.hex_x = int(hex[0:2])
        self.hex_y = int(hex[2:4])

    def __eq__(self, other):
        return self.hex == other.hex and self.sector == other.sector
    
    def __hash__(self) -> int:
        return hash(self.__str__())
    
    def __str__(self) -> str:
        return f"{self.sector}-{self.hex}"
    
    def __repr__(self) -> str:
        return self.__str__()

    def distance(self, other):
        if self.sector != other.sector:
            raise Exception(f"Unable to get distance between hexes in different sectors, {self} and {other}")

        x1 = self.hex_x
        y1 = self.hex_y
        x2 = other.hex_x
        y2 = other.hex_y
        return round((((x1 - x2) ** 2) + ((y1-y2) ** 2)) ** (1/2))

NEU_BAYERN = [SectorHex("Reft", "1822"), SectorHex("Reft", "1923")]
AMONDIAGE = [SectorHex("Reft", "2325"), SectorHex("Reft", "2225")]

class DataLoader:
    def __init__(self, max_jump) -> None:
        self.__world_cache = dict()
        self.__max_jump = max_jump

        self.__trade_goods = None
        self.__passage_freight = None
        self.__passenger_count = None
        self.__modified_price = None
        self.__life_support = None

    @staticmethod
    def __jump_worlds(sector, hex, max_jump):
        file_name = f"cache/{sector}-{hex}-{max_jump}.json"

        if os.path.isfile(file_name):
            with open(file_name, 'r') as file:
                return json.load(file)
        
        # Make sure cache dir exists
        Path("cache").mkdir(parents=True, exist_ok=True)

        r = requests.get(f'https://travellermap.com/api/jumpworlds?sector={sector}&hex={hex}&jump={max_jump}')
        jump_data = r.json()

        with open(file_name, 'w') as f:
            json.dump(jump_data, f)

        return jump_data


    def load_world_data(self, sector_hex, force=False):
        if force or sector_hex not in self.__world_cache:
            jump_data = self.__jump_worlds(sector_hex.sector, sector_hex.hex, self.__max_jump)
            current_world = self.__world_cache.get(sector_hex)
            other_worlds = []

            for raw_world_data in jump_data["Worlds"]:
                world = World(raw_world_data, self)

                if world.sector_hex == sector_hex:
                    if current_world is None:
                        current_world = world
                        self.__world_cache[current_world.sector_hex] = current_world
                elif world.sector_hex in self.__world_cache:
                    other_worlds.append(self.__world_cache[world.sector_hex])
                else:
                    other_worlds.append(world)
                    self.__world_cache[world.sector_hex] = world

            current_world.neighbours = other_worlds

        return self.__world_cache[sector_hex]
    
    def trade_goods(self):
        if self.__trade_goods is None:
            self.__trade_goods = []
            with open('tradeGoods.json', 'r') as file:
                tradeGoodsRaw = json.load(file)
                for tradeGoodRaw in tradeGoodsRaw:
                    self.__trade_goods.append(TradeGood(tradeGoodRaw, self))

        return self.__trade_goods
    
    def life_support(self, level):
        if self.__life_support is None:
            with open('lifeSupport.json', 'r') as file:
                self.__life_support = json.load(file)

        return self.__life_support[level]

    def passenger_count(self, roll):
        if roll < 1:
            roll = 1
        elif roll > 20:
            roll = 20

        if self.__passenger_count is None:
            with open('passengerCount.json', 'r') as file:
                self.__passenger_count = json.load(file)

        return self.__passenger_count[str(roll)] * AVERAGE_D6


    def modified_price(self, roll, type):
        if roll < -3:
            roll = -3
        elif roll > 25:
            roll = 25

        if self.__modified_price is None:
            with open('modifiedPrice.json', 'r') as file:
                self.__modified_price = json.load(file)

        return self.__modified_price[str(roll)][type]

    def passage(self, type, distance):
        if self.__passage_freight is None:
            with open('passageFreight.json', 'r') as file:
                self.__passage_freight = json.load(file)

        return self.__passage_freight[str(distance)][type]
    
class CompleteCondition:
    def __init__(self, destination=None, max_profit=None, max_duration=None) -> None:
        self.destination= destination
        self.max_profit = max_profit
        self.max_duration = max_duration

        if destination is None and max_profit is None and max_duration is None:
            raise Exception("Complete condition is not finished")

    def is_complete(self, world, total_duration, profit):
        if self.destination and self.destination.sector_hex == world.sector_hex:
            return True
        
        if self.max_profit is not None and profit >= self.max_profit:
            print(f"Yes Profit {profit} >= {self.max_profit} on {world.name}")
            return True
        
        if self.max_duration is not None and total_duration >= self.max_duration:
            return True
        
        return False

STARTING_NET_WORTH = "STARTING_NET_WORTH"

class Route:
    def __init__(self, starting_capital, starting_net_worth, worlds, avoid, complete_condition, ship, data_loader, start_duration, route_duration = 0, state=dict(),profit =0, text=[]) -> None:
        self.profit = profit
        self.starting_capital = starting_capital
        self.starting_net_worth = starting_net_worth
        self.complete_condition = complete_condition
        self.complete = complete_condition.is_complete(worlds[-1], route_duration, profit)
        self.state = state
        self.start_duration = start_duration
        self.worlds = worlds
        self.avoid = avoid
        self.text = text
        self.ship = ship

        self.data_loader = data_loader
        self.route_duration = route_duration
        self.total_duration = route_duration + start_duration

    def generate_next_steps(self):
        if self.complete:
            return []

        current_world = self.worlds[-1]
        trade_goods = self.data_loader.trade_goods()
        starting_world = self.total_duration == 0

        for other_world in current_world.neighbours:
            if self.complete_condition.destination and other_world in self.worlds:
                continue

            if self.worlds[-10:].count(other_world) > 1:
                continue

            if other_world in self.avoid:
                continue
            if len(current_world.neighbours) > 2 and len(self.worlds) > 1 and self.worlds[-2] == other_world:
                continue

            if other_world.zone == "R":
                continue

            if other_world.size is None:
                continue

            for allegiance in self.ship.banned_allegiances:
                if other_world.allegiance.startswith(allegiance):
                    continue

            distance = current_world.distance(other_world)

            if distance > self.ship.max_jump():
                continue

            text = []
            capital = self.starting_capital + self.profit
            cost = self.ship.fuel_cost(distance)
            text.append(f"Buy unrefined fuel for {cost}, capital {capital:,.2f}->{capital - cost:,.2f}")
            capital -= cost
            duration = self.ship.jumps_required(distance) + 1
            total_duration = self.total_duration + duration
            state = self.state.copy()

            if math.floor(self.total_duration / 4) < math.floor(total_duration / 4):
                text.append(f"Ship Maintenance paid of {self.ship.monthly_maint:,.2f}, capital: {capital:,.2f}->{capital-self.ship.monthly_maint:,.2f}")
                capital -= self.ship.monthly_maint

                life_support = self.ship.monthly_life_support(self.data_loader)
                text.append(f"Ship Life Support paid of {life_support:,.2f}, capital: {capital:,.2f}->{capital-life_support:,.2f}")
                capital -= life_support

                if self.ship.contract:
                    income = self.ship.contract.monthly_income()

                    if income > 0:
                        text.append(f"Monthly Income of {income:,.2f}, capital: {capital:,.2f}->{capital+income:,.2f}")
                        capital += income

                    mortgage_payment = self.ship.contract.mortgage_payment(state)
                    if mortgage_payment > 0:
                        text.append(f"Mortgage paid of {mortgage_payment:,.2f}, capital: {capital:,.2f}->{capital-mortgage_payment:,.2f}")
                        capital -= mortgage_payment

            passenger_revenue, description = current_world.passengers(other_world, self.ship, starting_world)

            if passenger_revenue > 0: 
                text.append(f"{description}, capital {capital:,.2f}->{passenger_revenue + capital:,.2f}")
                capital += passenger_revenue 

            starting_capital, final_capital, deals = current_world.best_trades(other_world, trade_goods, self.ship, capital, starting_world)

            if starting_capital is None:
                continue

            text += deals

            if self.ship.contract:
                cut, description = self.ship.contract.profit_cut(state, other_world, starting_capital, final_capital)

                if cut is not None:
                    text.append(description)
                    final_capital -= cut
            else:
                text.append(f"No Maint or mortgage as we go from {self.total_duration}->{total_duration}")

            if final_capital < 0:
                continue

            new_net_worth = final_capital

            if self.ship.contract:
                new_net_worth -= self.ship.contract.current_cut(state)
                
            text = self.text + [f"{bcolors.BOLD}{current_world.name} -> {other_world.name}{bcolors.ENDC} ({distance} hexes, {duration} weeks) {other_world.sector_hex} net worth {self.net_worth():,.2f} -> {new_net_worth:,.2f}"] + text
            yield Route(self.starting_capital, self.starting_net_worth, self.worlds.copy() + [other_world],self.avoid, self.complete_condition, self.ship, self.data_loader, self.start_duration, total_duration,state, final_capital - self.starting_capital, text)

    def projected_duration(self):
        if self.complete or not self.complete_condition.destination:
            return self.route_duration
        
        remaining_distance = self.worlds[-1].distance(self.complete_condition.destination)
        remaining_duration = self.ship.expected_duration(remaining_distance)
        return self.route_duration + remaining_duration

    def crow_flies(self):
        if self.complete or not self.complete_condition.destination:
            return self.route_duration

        return self.worlds[0].distance(self.complete_condition.destination)
    
    def net_worth(self):
        net_worth = self.profit + self.starting_capital

        if self.ship.contract:
            net_worth -= self.ship.contract.current_cut(self.state)

        return net_worth

    def real_profit(self):
        return self.net_worth() - self.starting_net_worth
    
    def profit_per_week(self):
        return self.real_profit() / self.route_duration

    def __lt__(self, other):
        if other is None:
            return True
        
        factor = self.projected_duration() / other.projected_duration()

        other_capital_normalised = other.profit_per_week() * factor
        return self.profit_per_week() > other_capital_normalised

    
    def __eq__(self, other):
        return False
        
def find_best_route(capital, net_worth, ship, data_loader, start, destination, start_duration,avoid, state):
    routes = [Route(capital, net_worth, [start], avoid, destination, ship, data_loader, start_duration, state=state)]
    heapq.heapify(routes)
    best_route = None
    completed_routes = 0

    while routes and completed_routes < 10:
        route = heapq.heappop(routes)

        for new_route in route.generate_next_steps():
            if new_route.complete:
                completed_routes += 1
                if new_route < best_route:
                    completed_routes = 0
                    best_route = new_route
            else:
                heapq.heappush(routes,new_route)
                routes.append(new_route)

    return best_route



class Passage:
    def __init__(self, type, number) -> None:
        self.type = type
        self.number = number

UNCUT_PROFITS = "uncut_profits"

class PerfectStrangerContract:
    def mortgage_payment(self, *args):
        return 0
    
    def monthly_income(self):
        return 0
    
    def current_cut(self, state):
        return state.get(UNCUT_PROFITS, 0) * .75
    
    def profit_cut(self, state, world, starting_capital, final_capital):
        if final_capital < starting_capital:
            return 0, "No profits to cut"
        
        profit = final_capital - starting_capital
        uncut_profit = state.get(UNCUT_PROFITS, 0)

        if world.sector_hex in NEU_BAYERN:
            state[UNCUT_PROFITS] = profit + uncut_profit
            return 0, f"No Bank of Amondiage in {world.name} uncut profits rise from {uncut_profit} to {uncut_profit + profit:,.2f}"
            
        cut = (profit + uncut_profit) *.75
        
        if uncut_profit > 0:
            del state[UNCUT_PROFITS]
            return cut, f"Stern Metal takes 75% ({cut:,.2f}) of the of total profits {uncut_profit + profit:,.2f} since last world with a Bank of Amondiage, capital: {final_capital:,.2f} -> {final_capital - cut:,.2f}"
        else:
            return cut, f"Stern Metal takes 75% of the of total profits, capital: {final_capital:,.2f} -> {final_capital - cut:,.2f}"

def parse_text(text):
    try:
        return float(text.replace(",", "").replace("%", ""))
    except ValueError:
        return text
    
def to_camel_case(s):
    # Split the string by spaces
    words = s.split(' ')
    # If there's only one word, return it as is
    if len(words) == 1:
        return words[0].lower()
    # Convert the first word to lowercase and capitalize the subsequent words
    camel_case = words[0].lower() + ''.join(word.capitalize() for word in words[1:])
    return camel_case

def parse_table(table):
    rows = table.find_all('tr')

    headers = None

    for row in rows:
        cells = row.find_all(['td', 'th'])
        if headers is None:
            headers = [to_camel_case(cell.get_text(strip=True)) for cell in cells]
        else:
            cell_values = [parse_text(cell.get_text(strip=True)) for cell in cells]
            yield dict(zip(headers, cell_values))

def get_md5_hash(text):
    # Create an MD5 hash object
    md5_hash = hashlib.md5()
    # Update the hash object with the text (encoded to bytes)
    md5_hash.update(text.encode('utf-8'))
    # Return the hexadecimal representation of the hash
    return md5_hash.hexdigest()

def get_trade_snapshot_html(url):
    cache_dir = "cache/tradeSnapshot"
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    hash = get_md5_hash(url)
    snapshot_file = f"{cache_dir}/{hash}"

    if os.path.isfile(snapshot_file):
        with open(snapshot_file, 'rb') as file:
            return file.read()
    
    with open(snapshot_file, 'wb') as file:
        r = requests.get(url)
        file.write(r.content)

    return r.content


def get_trade_snapshot(url):
    result = get_trade_snapshot_html(url)
    soup = BeautifulSoup(result, 'html.parser')
    header = soup.find('h3', string='Available Trade Goods')
    table = header.find_next('table')

    d = {
        "availableTradeGoods": list(parse_table(table))
    }

    header = soup.find('h3', string='Desired Goods')
    table = header.find_next('table')

    d["desiredGoods"] = list(parse_table(table))

    info = header.find_next("h3", string="Planet Info")
    d["planets"] = {}

    while info:
        planet_name = info.find_next('table').find_all("td")[0].get_text(strip=True)
        data = d["planets"][planet_name] = {}

        passengers = info.find_next("h3", string="Passengers").find_next("ul")
        data["passengers"] = {
            "high": len(passengers.find_all("div", string="Passage Desired: High")),
            "middle": len(passengers.find_all("div", string="Passage Desired: Middle")),
            "basic": len(passengers.find_all("div", string="Passage Desired: Basic")),
            "low": len(passengers.find_all("div", string="Passage Desired: Low")),
        }

        freight = info.find_next("h3", string="Freight").find_next("table")
        data["freight"] = list(parse_table(freight))

        info = info.find_next("h3", string="Planet Info")

    return d

def main():
    perfect_stranger = Ship(8946.84, 40, 1, 40, 12, 160, [Passage("low", 9), Passage("middle", 10)], PerfectStrangerContract(), 2, 2)
    solo_ship = Ship(3737, 10, 2, 20,18, 0, [Passage("middle", 1)], Mortgage(44840250), 2, 2)
    far_trader = Ship(4443, 40, 2, 40,63, 0, [Passage("low", 6),Passage("middle", 7)], Mortgage(53320500), 2, 2)
    empress_marava = Ship(4513, 40, 2, 40,57, 0, [Passage("low", 4),Passage("middle", 6)], Mortgage(54158200), 2, 2)
    booty_pirates_trader = Ship(5516, 20, 2, 20,66, 20, [], Mortgage(47610000), 2, 4, ["Im", "As"])

    ship = perfect_stranger
    data_loader = DataLoader(ship.max_jump())

    trade_snapshot = "https://travellertools.azurewebsites.net/Home/TradeInfo?sectorX=-3&sectorY=0&hexX=18&hexY=22&maxJumpDistance=5&brokerScore=2&advancedMode=False&illegalGoods=False&edition=Mongoose2&seed=1583474473&advancedCharacters=False&streetwiseScore=2&milieu=M1105"
    
    #start = data_loader.load_world_data(SectorHex("Trojan Reach", "2819"))
    #stops = [
    #]

    parsed_url = urlparse(trade_snapshot)
    query_params = parse_qs(parsed_url.query)
    maxJumpDistance = int(query_params.get('maxJumpDistance', [0])[0])

    if maxJumpDistance != ship.max_jump():
        print(f"Snapshot jump distance should be {ship.max_jump()} not {maxJumpDistance}")
        exit(1)

    start = data_loader.load_world_data(SectorHex("Reft", "1822"))

    if trade_snapshot:
        snapshot = get_trade_snapshot(trade_snapshot)
        start.set_trade_snapshot(snapshot)

    stops = [
        SectorHex("Reft", "1426"),
    ]

    avoid = [
    ]

    print(f"{bcolors.OKBLUE}Planning new Route{bcolors.ENDC}")
    
    stops = [data_loader.load_world_data(stop) for stop in stops]
    avoid = [data_loader.load_world_data(world) for world in avoid]
    
    capital = 1943650
    uncut_profits = capital - 165175
    profit = 0
    duration = 0
    max_profit = None
    max_duration = None
    percentage_increase = 0
    state = {
        UNCUT_PROFITS: uncut_profits
    }
    net_worth = capital

    if ship.contract:
        net_worth -= ship.contract.current_cut(state)

    for stop in stops:
        best_route = find_best_route(capital + profit,net_worth, ship, data_loader, start, CompleteCondition(stop), duration, avoid, state)
        state = best_route.state
        if best_route is None:
            print("Unable to find viable route")
            return

        print("\n".join(best_route.text))
        duration += best_route.route_duration
        percentage_increase += (duration * best_route.real_profit()) / (net_worth + profit)
        profit += best_route.real_profit()

        start = stop

    if max_profit is not None or max_duration is not None:
        best_route = find_best_route(capital, net_worth, ship, data_loader, start, CompleteCondition(max_profit=max_profit, max_duration=max_duration), duration, avoid)
        if best_route is None:
            print("Unable to find viable route")
            return
        duration = best_route.route_duration
        percentage_increase = (duration * best_route.real_profit()) / net_worth
        profit = best_route.real_profit()
        print("\n".join(best_route.text))
    
    print(f"Route takes {duration} weeks and a total profit of {profit:,.2f} which is {profit/duration:,.2f} or {percentage_increase/ duration:,.2f}% per week")
    

main()