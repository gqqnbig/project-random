const testGoogleAnalysis = Promise.all([fetch('https://www.googletagmanager.com/gtag/js?id=UA-83043759-2', {mode: 'no-cors'}),
	fetch('https://www.google-analytics.com/analytics.js', {mode: 'no-cors'})]).then(r => {
	return new Promise(resolve => resolve(true));
}).catch(reason => {
	return new Promise(resolve => resolve(false));
});


const searchResult = new Vue({
	el: "#searchResult",
	data: {
		apps: undefined,
		errorMessage: undefined,
		errorType: '',
		executionSeconds: undefined,
	},
	methods: {
		getPriceText(installFee, allowInAppPurchase) {
			if (installFee > 0)
				return "$" + installFee + (allowInAppPurchase ? "+" : "");
			else if (installFee == null || installFee == undefined)
				return "?"
			else
				return (allowInAppPurchase ? "" : "True ") + "free";
		},
		formatPermissions(permissions) {
			return Object.values(permissions).join('\n');
		}
	}
});

const searchingTimeoutHandler = setTimeout(() => searchResult.errorMessage = "Taking too long to load result. Something might be wrong.", 30 * 1000);

const startSearchTime = performance.now();
//When we got permission list and category list, then run the following code.
Promise.all([permissionPromise, categoryPromise, testGoogleAnalysis]).then(function (results) {
	if (results[2]) {
		//it is a session cookie because it doesn't have expiration.
		// `document.cookie =` is a special syntax that adds a SINGLE cookie.
		// But reading document.cookie will return ALL cookies.
		document.cookie = '_gaload=1';
	}
	fetch("/Api/Search?" + searchTool.queryString).then(r => r.json()).then(data => {
		if (data.error) {
			clearTimeout(searchingTimeoutHandler);
			searchResult.errorMessage = data.error;
			searchResult.errorType = "security";
		}
		else if (data.apps.length === 0) {
			clearTimeout(searchingTimeoutHandler);
			searchResult.errorMessage = "We couldn't find anything for your search.";
			searchResult.apps = '';
		}
		else {
			searchResult.apps = data.apps.map(d => {
				d.ratingTitle = 'Rating ' + d.rating.toFixed(1);
				if (d.num_reviews)
					d.ratingTitle += ` based on ${d.num_reviews} reviews`;
				return d;
			});

			searchResult.executionSeconds = (performance.now() - startSearchTime) / 1000;
			searchResult.errorMessage = undefined;

			//activate Google ads
			(adsbygoogle = window.adsbygoogle || []).push({});
		}
	});
});