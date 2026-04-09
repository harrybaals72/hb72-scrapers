In hb72-scrapers folder, create a scraper for adultism.com which scrapes scenebyfragment. The video page will be adultism.com/video/<id>. The ID is gotten from the filename, it will be a series of numbers after an underscore at the end of the filename but before the extension. Here's a real example: 
'Newest video out now_61782.mp4'. 
ID in this case is 61782. 
The webpage can show an age verification, it looks like this is part of the cookie after I accept it in my browser: ageOk=1
Here's the xpath details:
- Title is in an <h1 itemprop="name">
- Performer name is the text in an <a href="..." class="author-nick">
- Here's a real example of where to get the timestamp/date:
`<time itemprop="datePublished" datetime="2016-04-03T12:17:01+00:00" data-jts="1459685821000">Apr 3, 2016 at 12:17 pm</time>`
    - Please read other scrapers to see how this date can be handled if it's necessary
    - Alternatively, there's a <meta> with the upload date if that's easier. Real example of that here:
    `<meta itemprop="uploadDate" content="2016-04-03T12:17:01+0000">`
- Details are from text inside a <div> with itemprop="description". Real example:
`<div class="story" itemprop="description">
			Newest video out now
		</div>`

