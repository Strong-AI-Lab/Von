from setuptools import setup, find_packages

setup(
	name='tell_von',
	version='0.1.0',
	packages=find_packages(),
	install_requires=[
		# Dependencies here, if `tell_von` depends on [`vonlib`](command:_github.copilot.openSymbolFromReferences?%5B%7B%22%24mid%22%3A1%2C%22path%22%3A%22%2Fc%3A%2FUsers%2Fwitbr%2FOneDrive%2FDevelopment%2FExternal%2FSAILab%2FVon%2Fsrc%2Fvonlib%2F__init__.py%22%2C%22scheme%22%3A%22file%22%7D%2C%7B%22line%22%3A0%2C%22character%22%3A0%7D%5D "src/vonlib/__init__.py"):
		 'vonlib @ file:///C:/Users/witbr/OneDrive/Development/External/SAILab/Von/src/vonlib#egg=vonlib',
 
	],
	# Add all other necessary information here
)
