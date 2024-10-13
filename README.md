This code allows you to create the most profitable (capital growth per week) route using the travellermap system.
It can handle the following types of routes:
- List of Stops you want to make
- Max duration
- Travel until a level of capital is achieved

It projects income and profit generated using the following mechanics:
- Passengers
- Freight
- Specualtive trade

It can also handle regular expenditure for:
- Mortgage
- Fuel
- Maintenance
- Profit Cut to ship owner

## Features
- If you have a fuel bladder or some other space that can be optionally used for fuel the reduction in cargo will be accounted for
- Can avoid systems with particular allegiances if you are wanted in the imperium or similar
- Does not stop twice in the same system when stops are specified, or twice in the same month if not
- You can provide a traveller tools link for the planet you are starting from and it will use the items available there to calculate prices
- If no snapshot is provided or for systems after the first hop rolls of 3.5 on each D6 are assumed
- Will avoid bringing items between worlds if item is illegal in either start or destination system
- Will avoid bringing items if you can make more money on freight than profit on the speculative trade
- Accounts for Trade Good Modifiers based on Trade Codes of the start and destination planets for a given trade
- Will identify best combination of freight lots to fill remaining cargo when using a snapshot
- Will fill standard state rooms with basic passengers if not enough middle passengers are available
- Avoids restricted sectors
- When projecting passenger count uses trade codes that apply to start and destination planets
